# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
import hashlib
import io
import json
import os
import shutil
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import deploy
import scheduled_inputs as inputs


class InputTests(unittest.TestCase):
    def test_network_clients_receive_no_ambient_credentials(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "dummy", "KUBECONFIG": "production", "HELM_DRIVER": "unsafe",
                                     "GIT_CONFIG_COUNT": "1", "SSH_AUTH_SOCK": "agent", "DOCKERHUB_CURL": "external"}):
            env = inputs.public_env(Path("/tmp/acquire"))
        self.assertEqual(env["KUBECONFIG"], os.devnull)
        self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "https")
        self.assertFalse(set(env) & {"GITHUB_TOKEN", "SSH_AUTH_SOCK", "HELM_DRIVER", "GIT_CONFIG_COUNT", "DOCKERHUB_CURL"})

    def test_local_main_fetch_supplies_all_five_fingerprints_without_running_source(self):
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder)
            repo = tmp / "reviewed-main"
            repo.mkdir()
            paths = set(inputs.GUARDED) | {inputs.LOCK}
            for stack, _, names in deploy.COMPONENTS.values():
                base = Path("examples", "oci" if stack == "oci" else "otel-demo")
                paths.update(base / name for name in names)
            for name in paths:
                dest = repo / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(inputs.ROOT / name, dest)
            (repo / "unrelated-workflow.sh").write_text("exit 123")
            env = inputs.public_env(tmp)
            env["GIT_ALLOW_PROTOCOL"] = "file"  # A local-only source replaces public GitHub in this test.
            git = ["git", "-C", str(repo), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid"]
            for args in (["init", "-q", "-b", "main"], ["add", "."], ["commit", "-qm", "Synthetic reviewed tree"]):
                subprocess.run(git + args, env=env, check=True, capture_output=True)
            with patch.object(inputs, "CONFIG", str(repo)):
                revision = inputs.head(str(repo), env)
                exported = inputs.config_data(tmp, revision, env)
            inputs.verify_runtime(exported)
            self.assertFalse((exported / "unrelated-workflow.sh").exists())
            self.assertFalse((exported / ".git").exists())
            for component in deploy.COMPONENTS:
                self.assertEqual(deploy.fingerprint(component), deploy.fingerprint(component, exported))

    def test_ref_must_match_before_reading_any_blob(self):
        with patch.object(inputs, "fetch_tree", return_value=([], "1"*40)), patch.object(inputs, "public_command") as command:
            with self.assertRaisesRegex(ValueError, "main changed"):
                inputs.config_data(Path("/unused"), "2"*40, {})
            command.assert_not_called()

    def test_config_rejects_symlinks_and_oversized_blobs(self):
        name = sorted(inputs.GUARDED + (inputs.LOCK,))[0]
        for mode, size in (("120000", 20), ("100644", 8_000_000)):
            with self.subTest(mode=mode), patch.object(inputs, "COMPONENTS", {}), \
                    patch.object(inputs, "fetch_tree", return_value=([], "1"*40)), \
                    patch.object(inputs, "public_command", return_value=f"{mode} blob abc {size} {name}".encode()):
                with self.assertRaisesRegex(ValueError, "unsafe configuration"):
                    inputs.config_data(Path("/unused"), "1"*40, {})

    def test_reviewed_pins_are_the_only_executable_differences_accepted(self):
        script = "examples/otel-demo/deploy-all.sh"
        old = 'OPENSEARCH_CHART_VERSION="3.7.0"\nrun\n'
        change = old.replace("3.7.0", "3.8.0")
        self.assertEqual(inputs.compatible_script(script, old), inputs.compatible_script(script, change))
        self.assertNotEqual(inputs.compatible_script(script, old), inputs.compatible_script(script, change.replace("run", "other")))
        self.assertNotEqual(inputs.compatible_script("scripts/demo/scheduled_inputs.py", old),
                            inputs.compatible_script("scripts/demo/scheduled_inputs.py", change))
        with tempfile.TemporaryDirectory() as tmp, patch.object(inputs, "GUARDED", ("scripts/demo/scheduled_inputs.py",)):
            target = Path(tmp) / inputs.GUARDED[0]
            target.parent.mkdir(parents=True)
            target.write_text("changed")
            with self.assertRaisesRegex(ValueError, "matching executor"):
                inputs.verify_runtime(Path(tmp))

    def test_approval_and_preview_changes_require_a_matching_runtime_image(self):
        paths = {
            "scripts/demo/scheduled_approvals.py", "scripts/demo/scheduled_approval_types.py",
            "ops/executor/preview/main.go", "ops/executor/preview/go.mod", "ops/executor/preview/go.sum",
            "ops/executor/preview/check-abi.py", "ops/executor/Containerfile",
        }
        self.assertTrue(paths.issubset(inputs.GUARDED))
        with tempfile.TemporaryDirectory() as temp:
            baked, reviewed = Path(temp) / "image", Path(temp) / "reviewed"
            for name in paths:
                for root, value in ((baked, "original"), (reviewed, "changed")):
                    target = root / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(value)
                with self.subTest(name=name), patch.object(inputs, "ROOT", baked), patch.object(inputs, "GUARDED", (name,)):
                    with self.assertRaisesRegex(ValueError, "matching executor"):
                        inputs.verify_runtime(reviewed)

    def test_chart_updates_require_the_reviewed_archive_checksum(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "chart-1.0.0.tgz"
            path.write_bytes(b"reviewed")
            lock = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()}
            self.assertEqual(inputs.checked_archives([path], lock), {path.name: b"reviewed"})
            path.write_bytes(b"different")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                inputs.checked_archives([path], lock)
            with self.assertRaisesRegex(ValueError, "set differs"):
                inputs.checked_archives([], lock)

    def test_registry_must_agree_on_the_exact_linux_amd64_index(self):
        for platform, corrupt in (("amd64", False), ("arm64", False), ("amd64", True)):
            data = json.dumps({"schemaVersion": 2, "manifests": [{"platform": {"os": "linux", "architecture": platform},
                                                                 "digest": "sha256:"+"a"*64}]}).encode()
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            response = io.BytesIO(data)
            response.headers = {"Docker-Content-Digest": digest}
            if platform == "amd64" and not corrupt:
                inputs.verify_registry_manifest(response, digest)
            else:
                with self.assertRaises(ValueError):
                    inputs.verify_registry_manifest(response, "sha256:"+"f"*64 if corrupt else digest)

    def test_checkout_pins_have_exact_matching_locks(self):
        lock = json.loads((inputs.ROOT / inputs.LOCK).read_text())
        self.assertEqual({f"{chart}-{inputs.pin(inputs.ROOT, variable)}.tgz" for _, variable, chart, _ in inputs.CHARTS}, set(lock["charts"]))
        self.assertTrue(all(input_name.startswith(("scripts/", "examples/", "ops/", "docs/")) for input_name in inputs.GUARDED))
