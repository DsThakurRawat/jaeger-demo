# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Offline trust-boundary tests for the optional local monitoring rehearsal."""

import base64
import copy
import importlib.util
import io
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import yaml

FIXTURE = Path(__file__).resolve().parent / "rehearsals/monitoring-writer"


def load(name):
    spec = importlib.util.spec_from_file_location(name, FIXTURE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = load("build_fixture")
tickets = load("tickets")
with patch.dict(sys.modules, {"build_fixture": builder, "tickets": tickets}):
    driver = load("rehearse")


class MonitoringWriterTest(unittest.TestCase):
    def test_digest_covers_every_member(self):
        prefix = builder.ARTIFACT + "/"
        data = {prefix + "Chart.yaml": b"chart"}
        for i in range(17):
            for directory in ("templates", "artifacts"):
                data[f"{prefix}{directory}/{i:02}.yaml"] = b"fixture"

        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "fixture.tgz"

            def package(mtime, members):
                with tarfile.open(archive, "w:gz") as tar:
                    for name, value in members.items():
                        member = tarfile.TarInfo(name)
                        member.size = len(value)
                        member.mtime = mtime
                        tar.addfile(member, io.BytesIO(value))
                return builder.content_digest(archive)

            first = package(1, data)
            self.assertEqual(first, package(200, dict(reversed(list(data.items())))))
            self.assertNotEqual(
                first, package(1, {**data, prefix + "Chart.yaml": b"changed"})
            )
            with self.assertRaisesRegex(ValueError, "members"):
                package(1, {**data, prefix + "extra": b"extra"})

    def test_request_bound_to_prepared_inputs_and_uid(self):
        data = {
            "action": "upgrade",
            "requested": "false",
            "state": "prepared",
            "packageSHA256": "reviewed",
        }
        good = {"metadata": {"uid": "fixture"}, "data": dict(data, requested="true")}
        tickets.check_ticket(good, data, "fixture")
        for key, value in (
            ("action", "rollback"),
            ("state", "claimed"),
            ("requested", "false"),
            ("packageSHA256", "other"),
            ("additional", "unexpected"),
        ):
            obj = copy.deepcopy(good)
            obj["data"][key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                tickets.check_ticket(obj, data, "fixture")
        with self.assertRaises(RuntimeError):
            tickets.check_ticket(good, data, "different")
        with self.assertRaises(RuntimeError):
            tickets.check_ticket(
                {**good, "binaryData": {"extra": "eA=="}}, data, "fixture"
            )

    def test_only_dedicated_loopback_context(self):
        name = driver.CONTEXT
        cluster = {
            "server": "https://127.0.0.1:1234",
            "certificate-authority-data": base64.b64encode(
                b"-----BEGIN CERTIFICATE-----"
            ).decode(),
        }
        config = {
            "current-context": name,
            "contexts": [{"name": name, "context": {"user": name, "cluster": name}}],
            "users": [
                {
                    "name": name,
                    "user": {
                        "client-certificate-data": "fixture",
                        "client-key-data": "fixture",
                    },
                }
            ],
            "clusters": [{"name": name, "cluster": cluster}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config"
            path.write_text(yaml.safe_dump(config))
            driver.local_config(path, name)
            for bad in (
                "https://192.0.2.1:443",
                "https://127.0.0.1:1234/unexpected",
                "http://localhost:1234",
            ):
                cluster["server"] = bad
                path.write_text(yaml.safe_dump(config))
                with self.subTest(bad=bad), self.assertRaises(driver.Failure):
                    driver.local_config(path, name)

    def test_requester_role_singles_out_prepared_requests(self):
        docs = {
            obj["kind"]: obj
            for obj in yaml.safe_load_all(
                (FIXTURE / "trigger-policy.yaml").read_bytes()
            )
        }
        self.assertEqual(
            docs["Role"]["rules"],
            [
                {
                    "apiGroups": [""],
                    "resources": ["configmaps"],
                    "resourceNames": ["monitoring-upgrade", "monitoring-rollback"],
                    "verbs": ["get", "patch"],
                }
            ],
        )
        self.assertEqual(
            docs["RoleBinding"]["subjects"],
            [
                {
                    "kind": "ServiceAccount",
                    "namespace": tickets.NAMESPACE,
                    "name": tickets.REQUESTER,
                }
            ],
        )
        self.assertEqual(
            docs["ValidatingAdmissionPolicy"]["spec"]["failurePolicy"], "Fail"
        )
        self.assertEqual(
            docs["ValidatingAdmissionPolicyBinding"]["spec"]["validationActions"],
            ["Deny"],
        )
