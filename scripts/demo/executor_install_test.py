# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import importlib.util
from pathlib import Path
import unittest

from scheduled_receiver import ACCOUNTS, KUBECTL, Paths

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("executor_workloads", ROOT / "ops/executor/workloads.py")
workloads = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(workloads)
IMAGE = "ghcr.io/jaegertracing/jaeger-demo-executor@sha256:" + "a" * 64


CLUSTER_UID = "11111111-1111-4444-aaaa-bbbbbbbbbbbb"


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.objects = list(workloads.render(IMAGE, "oci-bv", CLUSTER_UID))
        self.by_kind = {v["kind"]: v for v in self.objects}

    def test_rejects_unpublished_references_and_tags(self):
        for ref in ("jaeger-demo/executor:latest", IMAGE.replace("ghcr.io", "localhost"), IMAGE[:-1]):
            with self.assertRaises(ValueError):
                list(workloads.render(ref, "oci-bv", CLUSTER_UID))

    def test_initial_install_has_one_uncredentialed_job_and_both_activations_off(self):
        self.assertTrue(self.by_kind["CronJob"]["spec"]["suspend"])
        self.assertEqual(self.by_kind["Deployment"]["spec"]["replicas"], 0)
        self.assertEqual(self.by_kind["CronJob"]["spec"]["timeZone"], "Etc/UTC")
        self.assertFalse(self.by_kind["Job"]["spec"]["template"]["spec"]["automountServiceAccountToken"])
        self.assertEqual(self.by_kind["PersistentVolumeClaim"]["spec"]["storageClassName"], "oci-bv")
        self.assertFalse({"RoleBinding", "ClusterRoleBinding", "Secret", "Service"} & self.by_kind.keys())

    def test_identity_keys_and_writable_paths_match_the_worker_role(self):
        paths = Paths()
        for mode in ACCOUNTS:
            spec = workloads.pod(IMAGE, mode, CLUSTER_UID)["spec"]
            self.assertEqual(spec["serviceAccountName"], ACCOUNTS[mode])
            self.assertTrue(spec["automountServiceAccountToken"])
            container, = spec["containers"]
            self.assertEqual(container["image"], IMAGE)
            self.assertEqual(container["env"], [{"name": "SCHEDULED_CLUSTER_UID", "value": CLUSTER_UID}])
            self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
            self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
            by_path = {v["mountPath"]: v for v in container["volumeMounts"]}
            self.assertEqual(by_path[str(paths.store)]["readOnly"], mode == "serve")
            self.assertTrue(by_path[str(paths.keys)]["readOnly"])
            self.assertEqual(str(paths.outcomes) in by_path, mode == "serve")
            secret, = [v["secret"] for v in spec["volumes"] if "secret" in v]
            keys = {item["key"] for item in secret["items"]}
            expected = {"decryption-key.pem"} if mode == "serve" else {"signing-key.pem", "recipient-public.pem"}
            self.assertEqual(keys, expected | {"verification-key.pem"})
            self.assertEqual(secret["secretName"], ACCOUNTS[mode] + "-keys")
        self.assertIn(str(Path(KUBECTL).parent) + "/", (ROOT / "ops/executor/Containerfile").read_text())
