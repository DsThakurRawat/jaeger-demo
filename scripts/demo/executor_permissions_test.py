# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import copy
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("executor_permissions", Path(__file__).resolve().parents[2] / "ops/executor/permissions.py")
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


class PermissionTests(unittest.TestCase):
    def test_read_stages_preserve_separate_workload_identities_and_secret_boundary(self):
        out = list(p.read_access())
        roles = [o for o in out if o["kind"] in ("Role", "ClusterRole")]
        self.assertTrue(all(set(r["verbs"]) <= {"get", "list", "watch"} for o in roles for r in o["rules"]))
        ledger = [r for r in roles if r["metadata"]["name"] == "jaeger-demo-release-ledger-read"]
        self.assertEqual({r["metadata"]["namespace"] for r in ledger}, set(p.NAMESPACES))
        self.assertTrue(all(r["rules"] == [p.rule("", "secrets", ["get", "list"])] for r in ledger))
        all_rules = [r for o in roles for r in o["rules"]]
        self.assertFalse(any("pods/exec" in r.get("resources", []) or "*" in r.get("resources", []) for r in all_rules))
        self.assertEqual(sum(len(r["resourceNames"]) for r in all_rules if r.get("resources") == ["customresourcedefinitions"]), 10)

    def test_default_writes_are_named_and_hook_create_is_separate(self):
        guards, roles = p.regular()
        granted = [r for o in roles if o["kind"] in ("Role", "ClusterRole") for r in o["rules"]]
        self.assertTrue(all(r["verbs"] == ["patch"] and r["resourceNames"] for r in granted))
        self.assertFalse(any("jobs" in r["resources"] or "customresourcedefinitions" in r["resources"] for r in granted))
        self.assertTrue(all(g["spec"].get("failurePolicy", "Fail") == "Fail" for g in guards))
        policies = [g for g in guards if g["kind"] == "ValidatingAdmissionPolicy"]
        for policy in policies:
            self.assertTrue(all(r["operations"] in (["UPDATE"], ["UPDATE", "DELETE"]) for r in policy["spec"]["matchConstraints"]["resourceRules"]))
        self.assertTrue(all("namespace" not in o["metadata"] for o in guards))
        self.assertTrue(set(x["name"] for o in roles if "subjects" in o for x in o["subjects"]) == {p.EXECUTOR})

    def test_optional_review_refuses_unknown_and_sensitive_resources_and_changed_uid(self):
        obj = p.resource("apps/v1", "Deployment", "frontend", "otel-demo", spec={"replicas": 1})
        obj["metadata"]["uid"] = "fixture"
        review = {"configurationSHA": "b"*40, "bundleSHA256": "c"*64, "slot": "2026-11-19T13:00:00Z", "changes":[
            {"namespace":"otel-demo", "group":"apps", "resource":"deployments", "name":"frontend", "states":[obj]}]}
        guards, roles = p.workload(review)
        self.assertEqual(roles[0]["rules"], [p.rule("apps", "deployments", ["patch"], ["frontend"])])
        check = guards[0]["spec"]["validations"][1]["expression"]
        self.assertIn(review["bundleSHA256"], check)
        for delta in ({"name":"unexpected"}, {"group":"","resource":"secrets"}, {"namespace":"other"}):
            invalid = copy.deepcopy(review)
            invalid["changes"][0].update(delta)
            with self.assertRaises(ValueError):
                p.workload(invalid)
        candidate = copy.deepcopy(obj)
        candidate["metadata"]["uid"] = "replacement"
        review["changes"][0]["states"].append(candidate)
        with self.assertRaises(ValueError):
            p.workload(review)
