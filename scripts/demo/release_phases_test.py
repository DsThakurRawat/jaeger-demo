# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import unittest

import release_phases as phases


class PhaseTests(unittest.TestCase):
    def setUp(self):
        self.ids = ["default/jaeger", "otel-demo/otel-demo"]
        self.rows = [{"id": rid} for rid in self.ids]
        self.statuses = dict.fromkeys(self.ids, "deployed")
        self.calls = []

    def apply(self, row, method):
        self.calls.append((method, row["id"]))

    def verify(self, stack):
        self.calls.append(("verify", stack))
        return True

    def run_rows(self):
        return phases.run(self.rows, self.statuses, self.apply, self.verify)

    def test_failed_retry_and_oci_gate_precede_otel(self):
        self.statuses[self.ids[0]] = "failed"
        receipt = self.run_rows()
        self.assertEqual(self.calls, [("sync", self.ids[0]), ("verify", "oci"),
                                      ("apply", self.ids[1]), ("verify", "otel")])
        self.assertEqual(receipt, {"releases": {self.ids[0]: "sync", self.ids[1]: "apply"},
                                   "verifiedStacks": ["oci", "otel"]})

    def test_pending_release_in_later_phase_stops_before_first_write(self):
        for state in ("pending-rollback", "pending-upgrade", "pending-install", "uninstalling",
                      "uninstalled", "superseded", "unknown", "absent"):
            self.statuses[self.ids[1]] = state
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, "operator review"):
                self.run_rows()
        self.assertEqual(self.calls, [])

    def test_oci_verification_failure_blocks_otel(self):
        self.verify = lambda stack: self.calls.append(("verify", stack)) and True
        with self.assertRaisesRegex(ValueError, "oci verification failed"):
            self.run_rows()
        self.assertEqual(self.calls, [("apply", self.ids[0]), ("verify", "oci")])

    def test_otel_only_requires_fresh_oci_health(self):
        self.rows = self.rows[1:]
        def failed(stack):
            self.assertEqual(stack, "oci")
            raise RuntimeError("OCI verification failed")
        self.verify = failed
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            self.run_rows()
        self.assertEqual(self.calls, [])

    def test_oci_gate_runs_when_no_oci_release_is_selected(self):
        self.rows = self.rows[1:]
        receipt = self.run_rows()
        self.assertEqual(self.calls, [("verify", "oci"), ("apply", self.ids[1]), ("verify", "otel")])
        self.assertEqual(receipt["verifiedStacks"], ["oci", "otel"])

    def test_release_absence_and_wrong_order_rejected(self):
        del self.statuses[self.ids[0]]
        with self.assertRaisesRegex(ValueError, "operator review"):
            self.run_rows()
        self.statuses[self.ids[0]] = "deployed"
        self.rows.reverse()
        with self.assertRaisesRegex(ValueError, "order"):
            self.run_rows()
        for rows in ([], [self.rows[1], self.rows[1]]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                phases.run(rows, self.statuses, self.apply, self.verify)
        self.assertEqual(self.calls, [])

    def test_absent_companion_is_installed_with_apply(self):
        self.rows.insert(1, {"id": "otel-demo/demo-resources"})
        self.run_rows()
        self.assertIn(("apply", "otel-demo/demo-resources"), self.calls)

    def test_helm_listing_maps_release_identities_to_statuses(self):
        listing = [{"name": "jaeger", "namespace": "default", "status": "failed", "revision": "3"},
                   {"name": "otel-demo", "namespace": "otel-demo", "status": "deployed", "revision": "1"}]
        self.assertEqual(phases.helm_statuses(listing),
                         {"default/jaeger": "failed", "otel-demo/otel-demo": "deployed"})
        with self.assertRaisesRegex(ValueError, "twice"):
            phases.helm_statuses(listing + listing[:1])


if __name__ == "__main__":
    unittest.main()
