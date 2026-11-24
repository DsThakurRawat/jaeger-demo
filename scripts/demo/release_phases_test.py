# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

from pathlib import Path
import re
import unittest

import release_phases as phases

ROOT = Path(__file__).resolve().parents[2]


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
                      "uninstalled", "superseded", "unknown", phases.ABSENT):
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
        self.statuses[self.ids[0]] = phases.ABSENT
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

    def test_confirmed_absent_companion_is_installed_with_apply(self):
        self.rows.insert(1, {"id": "otel-demo/demo-resources"})
        self.statuses["otel-demo/demo-resources"] = phases.ABSENT
        with self.assertRaisesRegex(ValueError, "operator review"):
            self.run_rows()
        self.assertEqual(self.calls, [])

    def test_confirmed_absent_companion_is_installed_only_when_requested_alone(self):
        companion = "otel-demo/demo-resources"
        statuses = {companion: phases.ABSENT}
        receipt = phases.run([{"id": companion}], statuses, self.apply, self.verify, install={companion})
        self.assertEqual(self.calls, [("verify", "oci"), ("install", companion), ("verify", "otel")])
        self.assertEqual(receipt["releases"], {companion: "install"})

    def test_install_refuses_everything_but_one_absent_companion_before_any_write(self):
        companion, other = "otel-demo/demo-resources", "jaeger/demo-resources"
        statuses = dict(self.statuses, **{companion: phases.ABSENT, other: phases.ABSENT})
        cases = [
            ([{"id": companion}, {"id": other}], {companion, other}, "only selected release"),
            (self.rows + [{"id": companion}], {companion}, "only selected release"),
            ([{"id": companion}], {companion, other}, "only selected release"),
            ([{"id": self.ids[1]}], {self.ids[1]}, "Only a demo-resources"),
            ([{"id": "otel-demo/other"}], {"otel-demo/other"}, "Only a demo-resources"),
        ]
        for rows, install, message in cases:
            with self.subTest(rows=rows, install=install), self.assertRaisesRegex(ValueError, message):
                phases.run(rows, statuses, self.apply, self.verify, install=install)
        for state in ("deployed", "failed", "unknown", "pending-install"):
            with self.subTest(state=state), self.assertRaisesRegex(ValueError, "not absent"):
                phases.run([{"id": companion}], {companion: state}, self.apply, self.verify, install={companion})
        # Without install, an absent release of any kind is refused.
        for rows in ([{"id": companion}], [{"id": "otel-demo/other"}]):
            with self.subTest(rows=rows), self.assertRaisesRegex(ValueError, "operator review"):
                phases.run(rows, {rows[0]["id"]: phases.ABSENT}, self.apply, self.verify)
        self.assertEqual(self.calls, [])

    def test_selected_release_without_a_status_is_refused_before_any_write(self):
        for key in (self.ids[1], "otel-demo/demo-resources"):
            rows = self.rows + [{"id": key}] if key not in self.ids else self.rows
            statuses = {k: v for k, v in self.statuses.items() if k != key}
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "no confirmed Helm status"):
                phases.run(rows, statuses, self.apply, self.verify)
        self.assertEqual(self.calls, [])

    def test_unknown_companion_omitted_from_the_listing_is_refused_before_any_write(self):
        companion = "otel-demo/demo-resources"
        self.rows.append({"id": companion})
        listing = {"default": [{"name": "jaeger", "namespace": "default", "status": "deployed"}],
                   "otel-demo": [{"name": "otel-demo", "namespace": "otel-demo", "status": "deployed"}]}
        read = {companion: "unknown"}
        statuses = phases.selected_statuses(self.rows, listing, read.__getitem__)
        self.assertEqual(statuses[companion], "unknown")
        with self.assertRaisesRegex(ValueError, "operator review"):
            phases.run(self.rows, statuses, self.apply, self.verify)
        self.assertEqual(self.calls, [])

    def test_only_a_named_not_found_read_makes_a_release_absent(self):
        listing = {"default": [{"name": "jaeger", "namespace": "default", "status": "failed"}],
                   "otel-demo": []}
        reads = []
        def read(key):
            reads.append(key)
            return None
        statuses = phases.selected_statuses(self.rows, listing, read)
        self.assertEqual(statuses, {self.ids[0]: "failed", self.ids[1]: phases.ABSENT})
        self.assertEqual(reads, [self.ids[1]])
        def unreadable(key):
            raise RuntimeError("Helm history failed")
        with self.assertRaisesRegex(RuntimeError, "history failed"):
            phases.selected_statuses(self.rows, listing, unreadable)

    def test_helm_listing_maps_release_identities_to_statuses(self):
        listing = {"default": [{"name": "jaeger", "namespace": "default", "status": "failed", "revision": "3"}],
                   "otel-demo": [{"name": "otel-demo", "namespace": "otel-demo", "status": "deployed", "revision": "1"}]}
        self.assertEqual(phases.helm_statuses(listing),
                         {"default/jaeger": "failed", "otel-demo/otel-demo": "deployed"})
        with self.assertRaisesRegex(ValueError, "twice"):
            phases.helm_statuses({"default": listing["default"] * 2})

    def test_listing_of_another_namespace_is_rejected(self):
        entry = {"name": "jaeger", "namespace": "default", "status": "deployed"}
        with self.assertRaisesRegex(ValueError, "appears in the listing of otel-demo"):
            phases.helm_statuses({"otel-demo": [entry]})
        with self.assertRaisesRegex(ValueError, "appears in the listing of otel-demo"):
            phases.selected_statuses(self.rows, {"default": [entry], "otel-demo": [entry]}, lambda key: None)

    def test_selected_release_needs_a_listing_of_its_namespace(self):
        entry = {"name": "jaeger", "namespace": "default", "status": "deployed"}
        reads = []
        with self.assertRaisesRegex(ValueError, "without a Helm release listing"):
            phases.selected_statuses(self.rows, {"default": [entry]}, reads.append)
        self.assertEqual(reads, [])

    def test_listing_is_per_namespace_and_names_only_release_namespaces(self):
        self.assertEqual(phases.list_args("otel-demo"), ["list", "-n", "otel-demo", "--max", "0", "-o", "json"])
        for namespace in phases.NAMESPACES:
            self.assertNotIn("--all-namespaces", phases.list_args(namespace))
        with self.assertRaisesRegex(ValueError, "not a release namespace"):
            phases.list_args("kube-system")

    def test_first_install_is_a_plain_non_atomic_install_of_one_namespace(self):
        args = phases.install_args("otel-demo", "chart.tgz", "values.json")
        self.assertEqual(args[:5], ["install", "demo-resources", "chart.tgz", "--namespace", "otel-demo"])
        self.assertIn("--rollback-on-failure=false", args)
        self.assertIn("--create-namespace=false", args)
        for flag in ("--take-ownership", "--atomic", "--install", "--rollback-on-failure=true"):
            self.assertNotIn(flag, args)
        with self.assertRaisesRegex(ValueError, "not a release namespace"):
            phases.install_args("kube-system", "chart.tgz", "values.json")

    def test_no_cluster_wide_helm_listing_remains_in_the_scripts(self):
        # A cluster-wide listing needs a cluster-wide Secret list, which the deployer role denies.
        pattern = re.compile(r"--all-namespaces|(?<![\w-])-A(?![\w-])")
        offenders = []
        for path in sorted((ROOT / "scripts").rglob("*")):
            if path.suffix not in {".py", ".sh", ".gotmpl", ".yaml", ".yml"} or path == Path(__file__).resolve():
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"\bhelm\b", line) and pattern.search(line):
                    offenders.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
