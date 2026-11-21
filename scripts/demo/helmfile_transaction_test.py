# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

from adoption_bundle import BundleError
from helmfile_transaction import reconcile, reconcile_companion, reconcile_existing


class TransactionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.kubeconfig = self.root / "fixture.yaml"
        self.kubeconfig.touch()
        (self.root / "bin").mkdir()
        for name in ("helmfile", "helm"):
            (self.root / "bin" / name).touch()
        self.row = {"id": "default/jaeger", "helm": {"operation": "upgrade", "atomic": True, "wait": "legacy",
                    "timeoutSeconds": 45, "historyMax": 3, "skipCRDs": True, "createNamespace": False}}
        self.files = {"values.yaml": b"private-value", "postrender.yaml": b"private-manifest", "chart.tgz": b"private-chart"}
        env = patch.dict(os.environ, {"JAEGER_HELMFILE_TOOLCHAIN": str(self.root), "HELMFILE_ENVIRONMENT": "untrusted",
                                      "HELM_DIFF_IGNORE_UNKNOWN_FLAGS": "true", "OCI_CLI_KEY_CONTENT": "synthetic"})
        env.start()
        self.addCleanup(env.stop)

    def call(self, method):
        reconcile_existing(self.row, method, self.files, kubeconfig=self.kubeconfig, context="fixture")

    def test_private_files_native_arguments_no_chart_source_resolution_and_clean_exit(self):
        paths = []
        def execute(command, *, cwd, env, stdin, stdout, stderr):
            paths.append(cwd)
            self.assertEqual((stdin, stdout, stderr), (subprocess.DEVNULL,) * 3)
            self.assertEqual((cwd.stat().st_mode & 0o777, env["HOME"], env["TMPDIR"]), (0o700, str(cwd), str(cwd)))
            self.assertFalse({"OCI_CLI_KEY_CONTENT", "HELMFILE_ENVIRONMENT", "HELM_DIFF_IGNORE_UNKNOWN_FLAGS"} & env.keys())
            self.assertEqual(Path(env["JAEGER_APPROVED_MANIFEST"]), cwd / "postrender.yaml")
            self.assertEqual(env["KUBECONFIG"], str(self.kubeconfig.resolve()))
            for name, content in self.files.items():
                self.assertEqual((cwd / name).read_bytes(), content)
                self.assertEqual((cwd / name).stat().st_mode & 0o777, 0o600)
            state = yaml.safe_load((cwd / "helmfile.yaml").read_bytes())
            self.assertEqual(state, {"helmDefaults": {"createNamespace": False, "historyMax": 3, "timeout": 45},
                                     "releases": [{"name": "jaeger", "namespace": "default", "chart": "./chart.tgz"}]})
            self.assertEqual(command[:3], [self.root / "bin/helmfile", "--helm-binary", self.root / "bin/helm"])
            self.assertIn("--skip-deps", command)
            self.assertEqual(command[command.index("--post-renderer") + 1], "verify-frozen-manifests")
            self.assertIn("--reset-values", command)
            sync, = [arg.split("=", 1)[1].split() for arg in command if str(arg).startswith("--sync-args=")]
            self.assertCountEqual(sync, ["--values=./values.yaml", "--install=false", "--wait=legacy",
                                  "--rollback-on-failure=true", "--skip-crds=true", "--create-namespace=false"])
            if "apply" in command:
                self.assertIn("--diff-args=--allow-unreleased=false --values=./values.yaml", command)
            return subprocess.CompletedProcess(command, 0)
        with patch("helmfile_transaction.subprocess.run", side_effect=execute):
            for method in ("apply", "sync"):
                self.call(method)
        self.assertTrue(all(not path.exists() for path in paths))

    def test_refuses_companions_first_install_missing_tools_and_unsafe_flags(self):
        with patch("helmfile_transaction.subprocess.run") as execute:
            for key, operation in (("default/demo-resources", "upgrade"), ("external/chart", "upgrade"),
                                   ("default/jaeger", "install")):
                self.row["id"], self.row["helm"]["operation"] = key, operation
                with self.assertRaises(BundleError):
                    self.call("sync")
            self.row["helm"]["operation"] = "upgrade"
            with self.assertRaises(BundleError):
                self.call("delete")
            self.row["helm"]["atomic"] = "--unknown"
            with self.assertRaises(BundleError):
                self.call("apply")
            self.row["helm"]["atomic"] = False
            with patch.dict(os.environ, {"JAEGER_HELMFILE_TOOLCHAIN": ""}), self.assertRaises(BundleError):
                self.call("sync")
            execute.assert_not_called()

    def companion(self, operation, **options):
        helm = dict(self.row["helm"], operation=operation, atomic=False, skipCRDs=False)
        return {"id": "opensearch/demo-resources", "helm": dict(helm, **options)}

    def test_first_companion_install_uses_pinned_helm_install_with_the_frozen_guard(self):
        paths = []
        def execute(command, *, cwd, env, stdin, stdout, stderr):
            paths.append(cwd)
            self.assertEqual((stdin, stdout, stderr), (subprocess.DEVNULL,) * 3)
            self.assertEqual((cwd.stat().st_mode & 0o777, env["HOME"], env["TMPDIR"]), (0o700, str(cwd), str(cwd)))
            self.assertFalse({"OCI_CLI_KEY_CONTENT", "HELMFILE_ENVIRONMENT", "HELM_DIFF_IGNORE_UNKNOWN_FLAGS"} & env.keys())
            self.assertEqual(Path(env["JAEGER_APPROVED_MANIFEST"]), cwd / "postrender.yaml")
            for name, content in self.files.items():
                self.assertEqual((cwd / name).read_bytes(), content)
                self.assertEqual((cwd / name).stat().st_mode & 0o777, 0o600)
            self.assertFalse((cwd / "helmfile.yaml").exists())
            self.assertEqual(command, [
                self.root / "bin/helm", "install", "demo-resources", "./chart.tgz", "--namespace", "opensearch",
                "--kubeconfig", str(self.kubeconfig.resolve()), "--kube-context", "fixture", "--values=./values.yaml",
                "--post-renderer", "verify-frozen-manifests", "--wait=legacy", "--timeout", "45s",
                "--rollback-on-failure=false", "--skip-crds=false", "--create-namespace=false"])
            return subprocess.CompletedProcess(command, 0)
        with patch("helmfile_transaction.subprocess.run", side_effect=execute):
            reconcile(self.companion("install"), "apply", self.files, kubeconfig=self.kubeconfig, context="fixture")
        self.assertEqual(1, len(paths))
        self.assertFalse(paths[0].exists())

    def test_installed_companion_uses_the_same_guarded_helmfile_upgrade_as_an_application(self):
        commands = []
        def execute(command, *, cwd, **kwargs):
            state = yaml.safe_load((cwd / "helmfile.yaml").read_bytes())
            commands.append((command[10], state["releases"], [a for a in command if str(a).startswith("--sync-args=")]))
            return subprocess.CompletedProcess(command, 0)
        with patch("helmfile_transaction.subprocess.run", side_effect=execute):
            for method in ("apply", "sync"):
                reconcile(self.companion("upgrade"), method, self.files, kubeconfig=self.kubeconfig, context="fixture")
        self.assertEqual(["apply", "sync"], [c[0] for c in commands])
        for _, releases, sync in commands:
            self.assertEqual([{"name": "demo-resources", "namespace": "opensearch", "chart": "./chart.tgz"}], releases)
            self.assertIn("--install=false", sync[0])

    def test_companion_refuses_applications_atomic_or_synced_installs_and_unsafe_flags(self):
        with patch("helmfile_transaction.subprocess.run") as execute:
            for row, method in ((self.row, "apply"), (self.companion("install", atomic=True), "apply"),
                                (self.companion("install"), "sync"), (self.companion("uninstall"), "apply"),
                                (self.companion("install", createNamespace=True), "apply"),
                                (self.companion("install", wait="--take-ownership"), "apply"),
                                (dict(self.companion("install"), id="kube-system/demo-resources"), "apply")):
                with self.subTest(row=row, method=method), self.assertRaises(BundleError):
                    reconcile_companion(row, method, self.files, kubeconfig=self.kubeconfig, context="fixture")
            # The dispatcher keeps application rows on the upgrade-only path.
            self.row["helm"]["operation"] = "install"
            with self.assertRaises(BundleError):
                reconcile(self.row, "apply", self.files, kubeconfig=self.kubeconfig, context="fixture")
            execute.assert_not_called()

    def test_failed_companion_install_does_not_disclose_private_input(self):
        failed = subprocess.CompletedProcess([], 1, stdout=b"private-output", stderr=b"private-error")
        with patch("helmfile_transaction.subprocess.run", return_value=failed), self.assertRaises(BundleError) as failure:
            reconcile_companion(self.companion("install"), "apply", self.files, kubeconfig=self.kubeconfig, context="fixture")
        self.assertEqual(str(failure.exception), "Frozen Helm install failed for opensearch/demo-resources (exit 1)")

    def test_failure_does_not_disclose_or_retain_private_input(self):
        paths = []
        def failed(command, *, cwd, **kwargs):
            paths.append(cwd)
            return subprocess.CompletedProcess(command, 1, stdout=b"private-output", stderr=b"private-error")
        with patch("helmfile_transaction.subprocess.run", side_effect=failed), self.assertRaises(BundleError) as failure:
            self.call("apply")
        self.assertEqual(str(failure.exception), "Frozen Helmfile apply failed for default/jaeger (exit 1)")
        self.assertFalse(paths[0].exists())
