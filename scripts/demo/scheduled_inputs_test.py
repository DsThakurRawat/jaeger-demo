# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import scheduled_inputs as inputs


class InputTests(unittest.TestCase):
    def test_network_clients_receive_no_ambient_credentials(self):
        with patch.dict(os.environ, {"GITHUB_TOKEN": "dummy", "KUBECONFIG": "production", "HELM_DRIVER": "unsafe",
                                     "GIT_CONFIG_COUNT": "1", "SSH_AUTH_SOCK": "agent", "DOCKERHUB_CURL": "external"}):
            env = inputs.public_env(Path("/tmp/acquire"))
        self.assertEqual(env["KUBECONFIG"], os.devnull)
        self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "https")
        self.assertFalse(set(env) & {"GITHUB_TOKEN", "SSH_AUTH_SOCK", "HELM_DRIVER", "GIT_CONFIG_COUNT", "DOCKERHUB_CURL"})

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
