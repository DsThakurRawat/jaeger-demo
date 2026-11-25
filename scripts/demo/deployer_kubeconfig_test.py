# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import contextlib
import io
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

import yaml

import deployer_kubeconfig

USER = "ocid1.user.oc1..aaaaexampledeployer"
ADMIN = {
    "apiVersion": "v1", "kind": "Config", "current-context": "other",
    "clusters": [{"name": "demo", "cluster": {"server": "https://demo.example.invalid:6443",
                                              "certificate-authority": "ca.crt"}},
                 {"name": "other", "cluster": {"server": "https://other.example.invalid:6443"}}],
    "users": [{"name": "admin", "user": {"exec": {"apiVersion": "client.authentication.k8s.io/v1beta1",
                                                  "command": "oci", "args": ["ce", "cluster", "generate-token"]},
                                         "client-key": "keys/admin.key"}},
              {"name": "other-admin", "user": {"token": "not-copied"}}],
    "contexts": [{"name": "demo-admin", "context": {"cluster": "demo", "user": "admin", "namespace": "default"}},
                 {"name": "other", "context": {"cluster": "other", "user": "other-admin"}}],
}


class DeployerKubeconfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.source = self.dir / "admin" / "config"
        self.source.parent.mkdir()
        self.source.write_text(yaml.safe_dump(ADMIN))
        self.output = self.dir / "deployer.kubeconfig"

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = deployer_kubeconfig.main(["--kubeconfig", str(self.source), "--context", "demo-admin",
                                                 "--output", str(self.output), *args])
            except SystemExit as exit:
                code = exit.code
        return code, out.getvalue()

    def test_writes_one_private_impersonating_context(self):
        code, out = self.run_main("--user", USER)
        self.assertEqual(0, code)
        self.assertNotIn(USER, out, "The OCID stays out of the output")
        self.assertEqual(0o600, stat.S_IMODE(self.output.stat().st_mode))
        config = yaml.safe_load(self.output.read_text())
        self.assertEqual("demo-admin", config["current-context"])
        self.assertEqual(["demo-admin"], [c["name"] for c in config["contexts"]])
        self.assertEqual({"cluster": "demo", "user": "admin", "namespace": "default"}, config["contexts"][0]["context"])
        self.assertEqual(["demo"], [c["name"] for c in config["clusters"]])
        user, = config["users"]
        self.assertEqual("admin", user["name"])
        self.assertEqual(USER, user["user"]["as"])
        self.assertNotIn("as-groups", user["user"])
        # The administrator's credential is kept; the other user's token is not copied.
        self.assertEqual(["ce", "cluster", "generate-token"], user["user"]["exec"]["args"])
        self.assertEqual("oci", user["user"]["exec"]["command"])
        self.assertNotIn("not-copied", self.output.read_text())

    def test_relative_files_resolve_against_the_source_kubeconfig(self):
        self.run_main("--user", USER)
        config = yaml.safe_load(self.output.read_text())
        base = self.source.resolve().parent
        self.assertEqual(str(base / "ca.crt"), config["clusters"][0]["cluster"]["certificate-authority"])
        self.assertEqual(str(base / "keys/admin.key"), config["users"][0]["user"]["client-key"])

    def test_groups_are_impersonated_when_given(self):
        self.assertEqual(0, self.run_main("--user", USER, "--group", "system:authenticated")[0])
        user = yaml.safe_load(self.output.read_text())["users"][0]["user"]
        self.assertEqual(["system:authenticated"], user["as-groups"])

    def test_refuses_anything_but_an_oci_user(self):
        for user in ("system:admin", "", "ocid1.group.oc1..aaaa", USER + "\n"):
            with self.subTest(user=user):
                with self.assertRaisesRegex(ValueError, "OCI user OCID"):
                    deployer_kubeconfig.impersonating(ADMIN, "demo-admin", user)
        self.assertEqual(1, self.run_main("--user", "system:admin")[0])
        self.assertFalse(self.output.exists())

    def test_refuses_a_missing_or_ambiguous_context(self):
        with self.assertRaisesRegex(ValueError, "0 context entries"):
            deployer_kubeconfig.impersonating(ADMIN, "absent", USER)
        twice = dict(ADMIN, contexts=ADMIN["contexts"] + [ADMIN["contexts"][0]])
        with self.assertRaisesRegex(ValueError, "2 context entries"):
            deployer_kubeconfig.impersonating(twice, "demo-admin", USER)
        dangling = dict(ADMIN, contexts=[{"name": "demo-admin", "context": {"cluster": "demo", "user": "nobody"}}])
        with self.assertRaisesRegex(ValueError, "0 user entries"):
            deployer_kubeconfig.impersonating(dangling, "demo-admin", USER)

    def test_refuses_a_user_entry_that_already_impersonates(self):
        users = [{"name": "admin", "user": {"token": "t", "as": "someone"}}]
        with self.assertRaisesRegex(ValueError, "already impersonates"):
            deployer_kubeconfig.impersonating(dict(ADMIN, users=users), "demo-admin", USER)

    def test_never_overwrites(self):
        self.output.write_text("keep")
        self.assertEqual(1, self.run_main("--user", USER)[0])
        self.assertEqual("keep", self.output.read_text())

    def test_refuses_a_kubeconfig_list(self):
        with self.assertRaisesRegex(ValueError, "single kubeconfig"):
            deployer_kubeconfig.write(f"{self.source}{os.pathsep}{self.source}", "demo-admin", USER, self.output)

    def test_invalid_yaml_is_refused_without_echoing_it(self):
        self.source.write_text("users: [ {token: secret-token-value\n")
        with self.assertRaises(ValueError) as caught:
            deployer_kubeconfig.write(self.source, "demo-admin", USER, self.output)
        self.assertEqual("The kubeconfig is not valid YAML", str(caught.exception))
        self.assertNotIn("secret-token-value", str(caught.exception))
        self.assertFalse(self.output.exists())

    def test_a_failed_write_leaves_no_partial_file(self):
        real_open = open

        def failing_open(file, *args, **kwargs):
            stream = real_open(file, *args, **kwargs)
            if isinstance(file, int):
                stream.write = lambda text: (_ for _ in ()).throw(OSError("disk full"))
            return stream

        with mock.patch("builtins.open", failing_open):
            with self.assertRaisesRegex(OSError, "disk full"):
                deployer_kubeconfig.write(self.source, "demo-admin", USER, self.output)
        self.assertFalse(self.output.exists())
        # The next run can write it.
        deployer_kubeconfig.write(self.source, "demo-admin", USER, self.output)
        self.assertTrue(self.output.exists())

    def test_does_not_change_its_input(self):
        before = yaml.safe_dump(ADMIN)
        deployer_kubeconfig.impersonating(ADMIN, "demo-admin", USER, ("system:authenticated",))
        self.assertEqual(before, yaml.safe_dump(ADMIN))


if __name__ == "__main__":
    unittest.main()
