# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import copy
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import scheduled_approval_types as t
import scheduled_approvals as a

SLOT = "2026-11-19T13:00:00Z"
RECEIPT = {"configurationSHA": "b" * 40, "bundleSHA256": "c" * 64}


class ProjectionTests(unittest.TestCase):
    @staticmethod
    def fixture():
        meta = {
            "name": "otel-collector-agent",
            "namespace": "otel-demo",
            "uid": "fixture",
            "resourceVersion": "1",
        }
        c = {
            "name": "collector",
            "image": "registry.invalid/collector:1",
            "env": [{"name": "CONFIG", "value": "safe"}],
        }
        return {
            "apiVersion": "apps/v1",
            "kind": "DaemonSet",
            "metadata": meta,
            "spec": {"template": {"spec": {"serviceAccountName": "read-only", "containers": [c]}}},
        }

    def test_collector_identity_command_injected_env_and_image_origin(self):
        old = self.fixture()
        candidate = copy.deepcopy(old)
        container = candidate["spec"]["template"]["spec"]["containers"][0]
        container["image"] = "registry.invalid/collector:2@sha256:" + "1" * 64
        a.safe_transition(old, candidate)
        for field, value in (
            ("command", ["new"]),
            ("env", []),
            ("securityContext", {"privileged": True}),
            ("image", "elsewhere/collector@sha256:" + "a" * 64),
            ("image", "registry.invalid/collector:2"),
        ):
            with self.subTest(field):
                modified = copy.deepcopy(candidate)
                modified["spec"]["template"]["spec"]["containers"][0][field] = value
                with self.assertRaises(ValueError):
                    a.safe_transition(old, modified)
        for field, value in (
            ("serviceAccountName", "replacement"),
            ("hostNetwork", True),
            ("volumes", [{"name": "new"}]),
        ):
            with self.subTest(field), self.assertRaises(ValueError):
                modified = copy.deepcopy(candidate)
                modified["spec"]["template"]["spec"][field] = value
                a.safe_transition(old, modified)

    def test_service_binding_stateful_storage_uid_and_status_projection(self):
        obj = self.fixture()
        other = copy.deepcopy(obj)
        other["metadata"]["resourceVersion"] = "2"
        other["status"] = {"unrelated": "controller output"}
        self.assertEqual(t.variant(obj), t.variant(other))
        other["metadata"]["uid"] = "replacement"
        with self.assertRaisesRegex(ValueError, "ownership"):
            a.safe_transition(obj, other)
        obj["kind"] = "StatefulSet"
        obj["metadata"]["namespace"] = "opensearch"
        obj["spec"]["replicas"] = 1
        other = copy.deepcopy(obj)
        other["spec"]["replicas"] = 2
        with self.assertRaisesRegex(ValueError, "storage"):
            a.safe_transition(obj, other)
        obj["kind"] = "Service"
        obj["spec"] = {"clusterIP": "10.0.1.2", "selector": {"app": "fixture"}}
        other = copy.deepcopy(obj)
        other["spec"]["selector"] = {}
        with self.assertRaisesRegex(ValueError, "binding"):
            a.safe_transition(obj, other)

    def test_signed_readback_requires_all_spec_and_uid_bytes_after_restart(self):
        parts = {
            t.name(s): {
                "metadata": {"uid": "id-" + s},
                "spec": dict(scope=s, slot=SLOT, **RECEIPT, requestUID="control", changes={}),
            }
            for s in t.SCOPES
        }
        parts["daily-hosted-update"] = {"metadata": {"uid": "control"}}
        get = lambda kind, name, ns: parts[name]
        with patch.object(a, "get", side_effect=get):
            digest = a.aggregate(SLOT, RECEIPT, "control")
            auth = SimpleNamespace(approval_commitment=Mock(return_value=digest))
            with patch.dict(sys.modules, scheduled_bundle=auth):
                self.assertIs(a.verify_approvals(SLOT, RECEIPT), True)
                auth.approval_commitment.assert_called_once_with(
                    a.STORE, SLOT, RECEIPT, verification_key=a.KEY
                )
                parts[t.name("jaeger")]["metadata"]["uid"] = "replacement"
                with self.assertRaisesRegex(ValueError, "sealed aggregate"):
                    a.verify_approvals(SLOT, RECEIPT)
                parts[t.name("jaeger")]["spec"]["slot"] = "2026-11-18T13:00:00Z"
                with self.assertRaisesRegex(ValueError, "incorrect"):
                    a.verify_approvals(SLOT, RECEIPT)

    def test_wrong_receipt_or_active_slot_denied_before_preparation(self):
        t.validate_receipt(SLOT, RECEIPT)
        for slot, receipt in (
            ("bad", RECEIPT),
            (SLOT, dict(RECEIPT, uri="external")),
            (SLOT, dict(RECEIPT, configurationSHA="bad")),
        ):
            with self.assertRaises(ValueError):
                t.validate_receipt(slot, receipt)
        for state in ("requested", "running", "verified", "failed", "ready"):
            record = {"metadata": {"uid": "uid"}, "data": {"slot": SLOT, "state": state}}
            with (
                patch.object(a, "get", return_value=record),
                patch.object(a, "prepare_variants") as prepare,
                self.assertRaises(ValueError),
            ):
                a.publish_approvals({}, SLOT, RECEIPT)
            prepare.assert_not_called()

    def test_true_crd_and_protected_resources_never_enter_approval_values(self):
        allowed = t.allowed()
        self.assertTrue(all(ns in t.SCOPES and r not in t.EXCLUDE for ns, _, r, n in allowed))
        self.assertFalse(any(ns == "default" and r == "daemonsets" for ns, _, r, _ in allowed))
        for _, _, r, _ in allowed:
            self.assertNotIn(
                r, {"customresourcedefinitions", "clusterroles", "jobs", "validatingwebhookconfigurations"}
            )

    def test_frozen_selection_history_and_live_baseline_before_preview(self):
        old = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "user-config", "namespace": "default"},
            "data": {"config": "old"},
        }
        new = copy.deepcopy(old)
        new["data"]["config"] = "new"
        live = copy.deepcopy(old)
        live["metadata"].update(uid="actual", resourceVersion="12")
        future = copy.deepcopy(live)
        future["data"]["config"] = "new"
        key, ident = "oci-jaeger", "v1:ConfigMap:default:user-config"
        fake = Mock()
        fake.RELEASES = {key: ("default", "jaeger", 20)}
        fake.component.side_effect = lambda x: x
        rollback = {"manifest": [old], "info": {"status": "deployed"}}
        records = [{"sha256": "1" * 64}]
        row = {
            "records": records,
            "applyMethod": "csa",
            "states": {ident: {"uid": "actual", "state": "hash"}},
            "rollbackRenderSHA256": t.digest({ident: old}),
        }
        plan = {"selected": [key], "images": {}, "releases": {key: row}}
        fake.validate_plan.return_value = plan
        fake.history.return_value = records, rollback, rollback
        fake.apply_method.return_value = "csa"
        fake.release_docs.side_effect = lambda r: r["manifest"]
        fake.index.side_effect = lambda objs, ns: {ident: o for o in objs}
        fake.material.return_value = b"chart", b"values", [new], []
        fake.inventory.return_value = {ident: new}
        fake.get_object.return_value = live
        fake.projection.return_value = row["states"][ident]
        with (
            patch.object(a, "client", return_value=fake),
            patch.object(a, "preview", return_value=future) as preview,
        ):
            wanted = {"uid": "actual", "variants": [t.variant(live), t.variant(future)]}
            self.assertEqual(
                a.prepare_variants({}, SLOT, RECEIPT)["default"], {"core/configmaps/user-config": wanted}
            )
            preview.assert_called_once_with(old, new, live, "default", "jaeger", "csa")
            fake.projection.return_value = {"uid": "foreign", "state": "wrong"}
            with self.assertRaisesRegex(ValueError, "drifted"):
                a.prepare_variants({}, SLOT, RECEIPT)
            fake.projection.return_value = row["states"][ident]
            fake.history.return_value = [{"sha256": "0" * 64}], rollback, rollback
            with self.assertRaisesRegex(ValueError, "History changed"):
                a.prepare_variants({}, SLOT, RECEIPT)
            fake.history.return_value = records, rollback, rollback
            plan["selected"] = []
            self.assertEqual(a.prepare_variants({}, SLOT, RECEIPT), {s: {} for s in t.SCOPES})

    def test_csa_uses_frozen_success_or_failed_original_and_never_downloads(self):
        obj = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": "user-config"},
            "data": {"a": "x"},
        }
        live = copy.deepcopy(obj)
        live["metadata"].update(namespace="default", uid="uid")
        fake = Mock()
        fake.command.return_value = t.canonical({"type": "merge", "patch": {}})
        with patch.object(a, "client", return_value=fake), patch.object(a, "dry_patch") as dry:
            self.assertEqual(a.preview(obj, obj, live, "default", "jaeger", "csa"), live)
            dry.assert_not_called()
            args, kwargs = fake.command.call_args
            self.assertEqual(args, ([a.CSA_HELPER],))
            sent = json.loads(kwargs["body"])
            self.assertEqual(sent["original"]["metadata"], {"name": "user-config", "namespace": "default"})
            self.assertEqual(
                sent["desired"]["metadata"]["annotations"]["meta.helm.sh/release-name"], "jaeger"
            )
            fake.command.return_value = t.canonical({"type": "unsafe", "patch": {}})
            with self.assertRaisesRegex(ValueError, "Invalid offline"):
                a.preview(obj, obj, live, "default", "jaeger", "csa")
