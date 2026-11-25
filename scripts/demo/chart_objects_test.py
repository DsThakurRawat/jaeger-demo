# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""The chart object check must agree with the inventory and name what it finds."""

import copy
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import chart_objects as c


DOCUMENT = json.loads(c.INVENTORY.read_text())
RECORDS = DOCUMENT["records"]
RELEASE_OF = {(component, release): namespace_release
              for namespace_release, component in c.COMPONENTS.items()
              for release in (namespace_release[1],)}
KINDS = {plural: kind for kind, plural in c.PLURALS.items()}
CLUSTER_KINDS = {kind.lower() + "s": kind for kind in c.CLUSTER}


def synthetic(row):
    """A minimal object with the identity that a reviewed record describes."""
    kind = CLUSTER_KINDS.get(row["resource"]) or KINDS.get(row["resource"]) or row["resource"][:-1].capitalize()
    metadata = {"name": row["name"]}
    if row["scope"] != "cluster":
        metadata["namespace"] = row["scope"]
    if row.get("hookEvents"):
        metadata["annotations"] = {c.HELM_HOOK: row["hookEvents"]}
    return {"apiVersion": row["apiGroup"] + "/v1" if row["apiGroup"] else "v1", "kind": kind, "metadata": metadata}


def render_records(records):
    """Run the collector over objects that reproduce the records. Returns the identities it finds.

    A history record stands for a Helmfile release, which gather() adds by its history_key.
    """
    found = {}
    index = c.collector(found, c.reviewed_rows(records))
    for key, row in c.reviewed_rows(records).items():
        if row["component"] == c.HISTORY:
            found[key] = {"kind": "Secret", "hook": ""}
            continue
        if row["release"] == c.SUPPLEMENT:
            namespace, release = row["scope"], "demo-resources"
        else:
            namespace, release = RELEASE_OF[row["component"], row["release"]]
        index(None, namespace, release, [synthetic(row)])
    return found


def fake_render(calls, change=None):
    """A stand-in for pr_render_diff.render that indexes objects reproducing RECORDS on either path.

    change maps "scripts" or "helmfile" to {"drop": record} or {"hook": record} to make that path differ.
    The Helmfile path indexes the supplemental objects as the demo-resources release of their namespace.
    """
    change = change or {}

    def render(root, temp, legacy, target, index, state="helmfile.yaml"):
        calls.append({"legacy": legacy, "target": target, "state": state})
        edit = change.get("scripts" if legacy else "helmfile", {})
        for row in c.reviewed_rows(RECORDS).values():
            if row["component"] == c.HISTORY or row is edit.get("drop"):
                continue
            obj = synthetic(row)
            if row is edit.get("hook"):
                obj["metadata"]["annotations"] = {c.HELM_HOOK: "post-delete"}
            if row["release"] == c.SUPPLEMENT:
                namespace, release = row["scope"], "demo-resources"
            else:
                namespace, release = RELEASE_OF[row["component"], row["release"]]
            index(None, namespace, release, [obj])
    return render


class ChartObjectsTests(unittest.TestCase):
    def test_every_reviewed_record_maps_back_to_its_own_key(self):
        reviewed = c.reviewed_rows(RECORDS)
        found = render_records(RECORDS)
        self.assertEqual(set(reviewed), set(found))
        self.assertEqual(([], [], []), c.compare(found, RECORDS))

    def test_only_the_checkpoint_is_outside_the_comparison_and_history_counts_per_release(self):
        objects = [row for row in RECORDS if row["component"] not in (c.HISTORY, c.CHECKPOINT)]
        releases = {(row["scope"], row["release"]) for row in RECORDS if row["component"] == c.HISTORY}
        reviewed = c.reviewed_rows(RECORDS)
        self.assertEqual(len(objects) + len(releases), len(reviewed), "every compared record has a distinct identity")
        self.assertEqual({c.history_key(*release) for release in releases},
                         {key for key in reviewed if key[0] == c.HISTORY})
        self.assertFalse([key for key in reviewed if key[0] == c.CHECKPOINT])

    def test_the_ten_helmfile_releases_have_history_records(self):
        # Guard against a vacuous pass: six applications and the four demo-resources companions.
        releases = {(row["scope"], row["release"]) for row in RECORDS if row["component"] == c.HISTORY}
        self.assertEqual(releases, set(c.COMPONENTS) | {(namespace, "demo-resources") for namespace in
                                                        ("default", "jaeger", "opensearch", "otel-demo")})

    def test_a_history_record_must_name_a_revision_of_its_release(self):
        row = next(r for r in RECORDS if r["component"] == c.HISTORY)
        for field, value in (("name", "sh.helm.release.v1.other.v3"), ("name", row["name"] + "x"),
                             ("resource", "configmaps"), ("apiGroup", "apps")):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, "not a Helm release record"):
                c.reviewed_rows([dict(row, **{field: value})])

    def test_every_pinned_release_has_a_component_and_records(self):
        self.assertEqual(6, len(c.COMPONENTS))
        for (namespace, release), component in c.COMPONENTS.items():
            with self.subTest(release=f"{namespace}/{release}"):
                self.assertTrue(any(row["component"] == component and row["release"] == release for row in RECORDS))

    def test_an_unreviewed_chart_object_is_missing_and_a_dropped_record_is_unused(self):
        row = next(r for r in RECORDS if r["release"] == "otel-demo")
        found = render_records(RECORDS)
        extra = copy.deepcopy(row)
        extra["name"] = row["name"] + "-unreviewed"
        found.update(render_records([extra]))
        missing, hooks, unused = c.compare(found, RECORDS)
        self.assertEqual([("otel-demo", "otel-demo", row["scope"], row["apiGroup"], row["resource"], extra["name"])], missing)
        self.assertEqual([], hooks)
        self.assertEqual([], unused)
        smaller = [r for r in RECORDS if r is not row]
        self.assertEqual([tuple(row[f] for f in c.FIELDS)], c.compare(render_records(RECORDS), smaller)[0])
        self.assertEqual([tuple(row[f] for f in c.FIELDS)], c.compare(render_records(smaller), RECORDS)[2])

    def test_hook_events_must_match_the_record(self):
        row = next(r for r in RECORDS if r.get("hookEvents"))
        key = tuple(row[f] for f in c.FIELDS)
        found = render_records(RECORDS)
        found[key] = dict(found[key], hook="pre-install")
        self.assertEqual([key], c.compare(found, RECORDS)[1])
        found[key] = dict(found[key], hook=",".join(reversed(row["hookEvents"].split(","))))
        self.assertEqual([], c.compare(found, RECORDS)[1], "event order is not a difference")
        found[key] = dict(found[key], hook="")
        self.assertEqual([key], c.compare(found, RECORDS)[1])

    def test_test_hooks_need_no_record_until_one_exists(self):
        hook = {"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "probe", "namespace": "default",
                                                                 "annotations": {c.HELM_HOOK: "test"}}}
        found = {}
        c.collector(found, {})(None, "default", "prometheus", [hook, None])
        self.assertEqual({}, found)
        key = ("oci-monitoring", "prometheus", "default", "", "pods", "probe")
        c.collector(found, {key: {}})(None, "default", "prometheus", [hook])
        self.assertEqual({key: {"kind": "Pod", "hook": "test"}}, found)
        mixed = dict(hook, metadata=dict(hook["metadata"], annotations={c.HELM_HOOK: "test,pre-upgrade"}))
        found = {}
        c.collector(found, {})(None, "default", "prometheus", [mixed])
        self.assertEqual(["pods"], [key[4] for key in found])

    def test_identity_follows_scope_plural_and_supplement_rules(self):
        role = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": "viewer", "namespace": "ignored"}}
        self.assertEqual(("oci-monitoring", "prometheus", "cluster", "rbac.authorization.k8s.io", "clusterroles", "viewer"),
                         c.row_key("default", "prometheus", role))
        ingress = {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": {"name": "front"}}
        self.assertEqual(("otel-jaeger", "jaeger", "jaeger", "networking.k8s.io", "ingresses", "front"),
                         c.row_key("jaeger", "jaeger", ingress))
        self.assertEqual(("", "supplement", "opensearch", "networking.k8s.io", "ingresses", "front"),
                         c.row_key("opensearch", "demo-resources", ingress))
        with self.assertRaises(KeyError):
            c.row_key("default", "unknown", ingress)

    def test_a_repeated_identity_is_refused(self):
        obj = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "twice", "namespace": "default"}}
        with self.assertRaisesRegex(ValueError, "same object identity"):
            c.collector({}, {})(None, "default", "prometheus", [obj, obj])

    def test_both_paths_render_upgrades_against_the_toolchain_lock_without_crds(self):
        lock = json.loads(c.TOOLS.read_text())["kubernetesSchema"]["version"]
        self.assertEqual(lock.lstrip("v"), c.kube_version())
        calls = []
        with patch.object(c.render_diff, "render", fake_render(calls)):
            found = c.gather(c.ROOT, RECORDS, c.kube_version(), c.chart_archives.LOCKED_STATE)
        self.assertEqual([True, False], [call["legacy"] for call in calls], "the script path and the Helmfile path")
        self.assertEqual(["--kube-version", c.kube_version(), "--is-upgrade"], calls[0]["target"])
        # helmfile template has no --is-upgrade; it passes it to helm template.
        self.assertEqual(["--kube-version", c.kube_version(), "--template-args=--is-upgrade"], calls[1]["target"])
        self.assertEqual(c.chart_archives.LOCKED_STATE, calls[1]["state"], "the lock-verified Helmfile state")
        for call in calls:
            self.assertNotIn("--include-crds", call["target"])
        self.assertEqual(render_records(RECORDS), found)
        self.assertEqual(([], [], []), c.compare(found, RECORDS))

    def test_the_two_paths_must_render_the_same_identities_and_hooks(self):
        row = next(r for r in RECORDS if r["release"] == "otel-demo" and not r.get("hookEvents"))
        hooked = next(r for r in RECORDS if r.get("hookEvents"))
        supplement = next(r for r in RECORDS if r["release"] == c.SUPPLEMENT)
        for name, change, expected in (
                ("an object only the script path renders", {"helmfile": {"drop": row}}, "only the script path renders"),
                ("an object only the Helmfile path renders", {"scripts": {"drop": row}}, "only the Helmfile path renders"),
                ("a supplement the companions do not own", {"helmfile": {"drop": supplement}}, "only the script path renders supplement"),
                ("different hook events", {"helmfile": {"hook": hooked}}, "hook events differ")):
            with self.subTest(name), patch.object(c.render_diff, "render", fake_render([], change)), \
                    self.assertRaisesRegex(ValueError, expected):
                c.gather(c.ROOT, RECORDS, c.kube_version(), c.chart_archives.LOCKED_STATE)

    def test_a_helmfile_release_without_history_records_is_missing(self):
        with patch.object(c.render_diff, "render", fake_render([])):
            found = c.gather(c.ROOT, RECORDS, c.kube_version(), c.chart_archives.LOCKED_STATE)
        smaller = [r for r in RECORDS if not (r["component"] == c.HISTORY and r["release"] == "demo-resources"
                                              and r["scope"] == "jaeger")]
        self.assertEqual([c.history_key("jaeger", "demo-resources")], c.compare(found, smaller)[0])

    def test_the_inventory_header_repeats_the_chart_lock(self):
        self.assertEqual(c.locked_archives(c.ROOT), DOCUMENT["chartArchiveSHA256"])
        self.assertEqual(json.loads((c.ROOT / c.chart_archives.LOCK).read_text())["charts"], c.locked_archives(c.ROOT))

    def test_the_inventory_keeps_the_versions_of_the_toolchain_lock(self):
        lock = json.loads(c.TOOLS.read_text())
        self.assertEqual(lock["tools"]["helm"]["version"].lstrip("v"), DOCUMENT["helm"])
        self.assertEqual(lock["kubernetesSchema"]["version"].lstrip("v"), DOCUMENT["targetKubernetes"])


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for source in (c.INVENTORY, c.TOOLS, c.ROOT / c.chart_archives.LOCK):
            target = self.root / source.relative_to(c.ROOT)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(source, target)
        self.path = self.root / c.INVENTORY.relative_to(c.ROOT)

    def tearDown(self):
        self.temp.cleanup()

    def run_main(self, mode, found, crds=None):
        out, err = io.StringIO(), io.StringIO()
        granted = c.granted_crds(c.deployer_access.ROLES)
        with patch.object(c, "locked_state", return_value=c.chart_archives.LOCKED_STATE), \
                patch.object(c, "gather", return_value=found) as gather, \
                patch.object(c, "rendered_crds", return_value=granted if crds is None else crds) as rendered, \
                patch.object(c, "granted_crds", return_value=granted), patch("sys.stderr", err):
            status = c.main([mode], out=out, root=self.root)
        # Both renders use the one lock-verified state.
        self.assertEqual(c.chart_archives.LOCKED_STATE, gather.call_args.args[3])
        self.assertEqual(c.chart_archives.LOCKED_STATE, rendered.call_args.args[2])
        return status, out.getvalue(), err.getvalue()

    def drifted(self):
        """Renders for an inventory that lost a chart record, a supplemental record and a hook event."""
        records = json.loads(self.path.read_text())["records"]
        found = render_records(records)
        chart = next(r for r in records if r["release"] == "opensearch")
        supplement = next(r for r in records if r["release"] == c.SUPPLEMENT)
        hook = dict(next(r for r in records if r.get("hookEvents")))
        document = json.loads(self.path.read_text())
        document["records"] = [r for r in document["records"] if r not in (chart, supplement)]
        hooked = next(r for r in document["records"] if r.get("hookEvents") == hook["hookEvents"] and r["name"] == hook["name"])
        hooked["hookEvents"] = "pre-install"
        self.path.write_text(json.dumps(document, indent=2) + "\n")
        return found, chart, supplement, hook

    def test_check_passes_when_the_inventory_matches_and_ignores_unused_records(self):
        records = json.loads(self.path.read_text())["records"]
        found = render_records(records)
        status, out, _ = self.run_main("--check", found)
        self.assertEqual(0, status)
        self.assertIn("Every rendered object and Helmfile release has a reviewed record.", out)
        found.popitem()
        status, out, _ = self.run_main("--check", found)
        self.assertEqual(0, status)
        self.assertIn("1 reviewed record(s) match no rendered object", out)

    def test_check_fails_naming_each_difference_without_writing(self):
        found, chart, supplement, hook = self.drifted()
        before = self.path.read_text()
        status, out, err = self.run_main("--check", found)
        self.assertEqual(1, status)
        self.assertIn(f"opensearch / opensearch: {chart['apiGroup'] or 'core'}/{chart['resource']} {chart['scope']}/{chart['name']}", out)
        self.assertIn(f"supplement: {supplement['apiGroup'] or 'core'}/{supplement['resource']} {supplement['scope']}/{supplement['name']}", out)
        self.assertIn(f"rendered '{hook['hookEvents']}'", out)
        self.assertIn("chart_objects.py --write", err)
        self.assertIn(c.PROCEDURE, err)
        self.assertEqual(before, self.path.read_text())

    def test_write_restores_the_records_and_a_second_check_passes(self):
        found, chart, supplement, hook = self.drifted()
        status, out, _ = self.run_main("--write", found)
        self.assertEqual(0, status)
        self.assertIn("Wrote 3 change(s)", out)
        self.assertEqual(0, self.run_main("--check", found)[0])
        written = json.loads(self.path.read_text())
        original = {tuple(r[f] for f in c.FIELDS): r for r in RECORDS}
        for row in written["records"]:
            key = tuple(row[f] for f in c.FIELDS)
            self.assertEqual(original[key]["verbs"], row["verbs"])
            self.assertEqual(original[key].get("hookEvents"), row.get("hookEvents"))
        self.assertEqual(len(RECORDS), len(written["records"]))
        # Helm history and the checkpoint stay last, and the file keeps its formatting.
        components = [r["component"] for r in written["records"]]
        first = min(i for i, name in enumerate(components) if name in ("history", "shared"))
        self.assertEqual({"history", "shared"}, set(components[first:]))
        self.assertEqual(json.dumps(written, indent=2, ensure_ascii=False) + "\n", self.path.read_text())

    def test_write_flags_protected_kinds_and_never_removes_records(self):
        records = json.loads(self.path.read_text())["records"]
        found = render_records(records)
        role = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": {"name": "new-role"}}
        found.popitem()
        found[c.row_key("default", "prometheus", role)] = {"kind": "ClusterRole", "hook": ""}
        status, out, _ = self.run_main("--check", found)
        self.assertEqual(1, status)
        self.assertIn("new-role (PROTECTED kind", out)
        self.assertEqual(0, self.run_main("--write", found)[0])
        after = json.loads(self.path.read_text())["records"]
        self.assertEqual(len(records) + 1, len(after))
        self.assertTrue(any(r["name"] == "new-role" and r["scope"] == "cluster" and r["verbs"] == c.STEADY_VERBS for r in after))

    def test_a_render_failure_is_reported_without_changing_the_inventory(self):
        before = self.path.read_text()
        err = io.StringIO()
        with patch.object(c, "locked_state", return_value=c.chart_archives.LOCKED_STATE), \
                patch.object(c, "gather", side_effect=ValueError("Public fixture preparation or rendering failed")), \
                patch("sys.stderr", err):
            self.assertEqual(2, c.main(["--check"], out=io.StringIO(), root=self.root))
        self.assertIn("Public fixture preparation or rendering failed", err.getvalue())
        self.assertEqual(before, self.path.read_text())

    def test_the_rendered_crds_must_equal_the_named_crd_grant(self):
        granted = c.granted_crds(c.deployer_access.ROLES)
        self.assertEqual(granted, set(c.deployer_access.MONITORING_CRDS))
        records = json.loads(self.path.read_text())["records"]
        found = render_records(records)
        status, out, _ = self.run_main("--check", found)
        self.assertEqual(0, status)
        self.assertIn(f"The {len(granted)} rendered CRDs match", out)
        changed = (granted - {"probes.monitoring.coreos.com"}) | {"widgets.monitoring.coreos.com"}
        for mode in ("--check", "--write"):
            with self.subTest(mode=mode):
                status, out, err = self.run_main(mode, found, crds=changed)
                self.assertEqual(1, status)
                self.assertIn("CRD rendered but not granted: widgets.monitoring.coreos.com", out)
                self.assertIn("CRD granted but not rendered: probes.monitoring.coreos.com", out)
                self.assertIn("named CRD get grant", err)

    def test_the_crd_render_includes_crds_with_the_toolchain_kubernetes_version(self):
        seen = {}

        def render(root, temp, legacy, target, index, state):
            seen.update(legacy=legacy, target=target, state=state)
            index(None, "default", "prometheus", [
                {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
                 "metadata": {"name": "probes.monitoring.coreos.com"}},
                {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "other"}}, None])

        with patch.object(c.render_diff, "render", render):
            self.assertEqual({"probes.monitoring.coreos.com"},
                             c.rendered_crds(c.ROOT, c.kube_version(), c.chart_archives.LOCKED_STATE))
        self.assertFalse(seen["legacy"], "the drift report reads what the Helmfile render owns")
        self.assertEqual(c.chart_archives.LOCKED_STATE, seen["state"])
        self.assertEqual(["--kube-version", c.kube_version(), "--include-crds"], seen["target"])

    def test_write_adds_history_records_last_for_a_new_release(self):
        records = json.loads(self.path.read_text())["records"]
        found = render_records(records)
        found[c.history_key("otel-demo", "next-release")] = {"kind": "Secret", "hook": ""}
        status, out, _ = self.run_main("--check", found)
        self.assertEqual(1, status)
        self.assertIn("history: Helm release records of otel-demo/next-release (sh.helm.release.v1.next-release.v<N>) "
                      "(PROTECTED kind", out)
        self.assertEqual(0, self.run_main("--write", found)[0])
        written = json.loads(self.path.read_text())["records"]
        self.assertEqual(dict(component=c.HISTORY, release="next-release", scope="otel-demo", apiGroup="",
                              resource="secrets", name="sh.helm.release.v1.next-release.v1", verbs=c.HISTORY_VERBS),
                         {k: v for k, v in written[-1].items() if k != "source"})
        self.assertEqual(0, self.run_main("--check", found)[0])

    def test_a_stale_chart_archive_list_fails_the_check_and_write_copies_the_lock(self):
        document = json.loads(self.path.read_text())
        document["chartArchiveSHA256"] = {"opensearch-3.7.0.tgz": "0" * 64}
        self.path.write_text(json.dumps(document, indent=2) + "\n")
        found = render_records(document["records"])
        status, out, _ = self.run_main("--check", found)
        self.assertEqual(1, status)
        self.assertIn("chartArchiveSHA256 differs from the charts of scripts/demo/chart-archives.lock.json", out)
        status, out, _ = self.run_main("--write", found)
        self.assertEqual((0, True), (status, "Wrote 1 change(s)" in out))
        lock = json.loads((self.root / c.chart_archives.LOCK).read_text())["charts"]
        self.assertEqual(lock, json.loads(self.path.read_text())["chartArchiveSHA256"])
        self.assertEqual(0, self.run_main("--check", found)[0])

    def test_the_locked_state_is_pulled_with_an_empty_helm_configuration(self):
        seen = {}

        def locked_state(root, dest, run):
            seen.update(root=root, dest=dest)
            run(["helm", "pull"], stdout=None)

        with patch.object(c.chart_archives, "locked_state", locked_state), \
                patch.object(c.subprocess, "run", lambda args, **kwargs: seen.update(args=args, **kwargs)):
            self.assertEqual(c.chart_archives.LOCKED_STATE, c.locked_state(self.root))
        self.assertEqual((self.root, self.root / c.ARCHIVES), (seen["root"], seen["dest"]))
        self.assertTrue(seen["dest"].is_dir())
        env = seen["env"]
        self.assertEqual(c.os.devnull, env["KUBECONFIG"])
        for name in ("HELM_CONFIG_HOME", "HELM_CACHE_HOME", "HELM_DATA_HOME"):
            self.assertIn("chart-objects-helm-", env[name])
        self.assertEqual(["helm", "pull"], seen["args"])

    def test_a_mode_is_required(self):
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            c.main([], root=self.root)


if __name__ == "__main__":
    unittest.main()
