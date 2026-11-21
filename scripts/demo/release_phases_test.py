# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import copy
import unittest

import release_phases as phases


class PhaseTests(unittest.TestCase):
    def setUp(self):
        self.ids = ["default/jaeger", "otel-demo/otel-demo"]
        self.rows = [{"id": rid, "observe": [f"object-{i}"], "rollback": {"revision": 3}}
                     for i, rid in enumerate(self.ids)]
        self.plan = {"releases": self.rows, "images": {"oci": {}}}
        self.baseline = {"cluster": "fixture", "healthy": True, "capabilitiesSHA256": "fixture",
                         "objects": {"object-0": "before", "object-1": "before"},
                         "releases": {rid: {"status": "deployed"} for rid in self.ids},
                         "rollbackTargets": dict.fromkeys(self.ids, 3), "images": {"oci": {}}}
        self.live = copy.deepcopy(self.baseline)
        self.calls = []

    def observe(self, scope):
        self.calls.append(("observe", scope["releases"]))
        return phases.baseline_for(scope, self.live)

    def apply(self, row, method):
        self.calls.append((method, row["id"]))
        self.live["objects"][row["observe"][0]] = "changed"

    def verify(self, stack):
        self.calls.append(("verify", stack))
        return True

    def run_plan(self):
        return phases.run(self.plan, self.baseline, self.observe, self.apply, self.verify)

    def test_failed_retry_and_oci_gate_precede_otel_even_after_oci_changed(self):
        self.live["releases"][self.ids[0]]["status"] = "failed"
        self.baseline = copy.deepcopy(self.live)
        receipt = self.run_plan()
        self.assertEqual(self.calls, [("observe", self.ids[:1]), ("sync", self.ids[0]), ("verify", "oci"),
                                     ("observe", self.ids[1:]), ("apply", self.ids[1]), ("verify", "otel")])
        self.assertEqual(receipt["verifiedStacks"], ["oci", "otel"])

    def test_pending_release_in_later_phase_stops_before_first_write(self):
        self.baseline["releases"][self.ids[1]]["status"] = "pending-rollback"
        with self.assertRaisesRegex(ValueError, "operator review"):
            self.run_plan()
        self.assertEqual(self.calls, [])

    def test_otel_drift_after_oci_verification_blocks_otel(self):
        def drift(stack):
            self.live["objects"]["object-1"] = "unexpected"
            return True
        self.verify = drift
        with self.assertRaisesRegex(ValueError, "otel changed"):
            self.run_plan()
        self.assertNotIn(("apply", self.ids[1]), self.calls)

    def test_otel_only_requires_fresh_oci_health(self):
        del self.baseline["releases"][self.ids[0]]
        self.plan["releases"] = self.rows[1:]
        def failed(stack):
            self.assertEqual(stack, "oci")
            raise RuntimeError("OCI verification failed")
        self.verify = failed
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            self.run_plan()
        self.assertEqual(self.calls, [])

    def test_release_absence_and_wrong_order_rejected(self):
        self.baseline["releases"][self.ids[0]]["status"] = "absent"
        with self.assertRaisesRegex(ValueError, "operator review"):
            self.run_plan()
        self.baseline["releases"][self.ids[0]]["status"] = "deployed"
        self.plan["releases"].reverse()
        with self.assertRaisesRegex(ValueError, "order"):
            self.run_plan()
        self.assertEqual(self.calls, [])
