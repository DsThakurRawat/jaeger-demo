# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

from contextlib import ExitStack
import json
import os
import sys
import unittest
from unittest.mock import patch

import scheduled_adapter as a


COMMIT = "a" * 40
SLOT = "2026-11-19T13:00:00Z"


def images():
    values = {}
    for key, repo, digit in (("jaeger", "jaegertracing/jaeger-snapshot", "b"), ("hotrod", "jaegertracing/example-hotrod-snapshot", "c")):
        tag, dg = digit * 40, "sha256:" + digit * 64
        values[key] = {"tag": tag, "sourceCommit": tag, "repository": repo, "digest": dg, "reference": f"{repo}:{tag}@{dg}", "platformManifest": dg}
    return values


def obj(kind, name, **data):
    return {"apiVersion": "v1", "kind": kind, "metadata": {"name": name}, **data}


class SimulatedCluster:
    def __init__(self, tests, initial=None):
        self.stack = ExitStack()
        tests.addCleanup(self.stack.close)
        self.image_map = images()
        self.sha = {k: a.deploy.fingerprint(k) for k in a.COMPONENTS}
        self.checks = dict(initial or dict.fromkeys(a.COMPONENTS))
        self.versions = dict.fromkeys(a.RELEASES, 7)
        self.state = dict.fromkeys(a.RELEASES, "original")
        self.seen = []
        self.fail_writes = None
        self.fail_health_when = None
        self.lost_lock = False
        self.pvc = ["data", "unchanged-uid", "unchanged-pv"]
        self.charts = {ns + "/" + name: key.encode() for key, (ns, name, _) in a.RELEASES.items()}
        def bind(name, fn):
            self.stack.enter_context(patch.object(a, name, side_effect=fn))
        bind("check_context", lambda: {"clusterUID": "fake", "kubernetes": "v1.36.1"})
        bind("checkpoint", lambda: ("state-uid", self.checks.copy()))
        bind("bindings", lambda: self.pvc.copy())
        bind("history", self.history)
        bind("helm_transaction", self.transaction)
        bind("baseline", self.baseline)
        bind("check_jaeger_patch_access", lambda ns, candidate: self.seen.append(("patch-access", ns)))
        bind("check_monitoring_workloads", lambda *, wait: self.seen.append(("monitoring-ready", wait)))
        bind("patch_supplemental", lambda o, prior, locked: self.seen.append(("patch", o["kind"])))
        bind("rollback_policy", lambda k, r: (False, {}))
        bind("ready_images", lambda key, sources: self.seen.append(("ready-images", key)))
        bind("rollout", lambda key: self.seen.append(("rollout", key)))
        bind("verify_upgrade", lambda *args: self.seen.append(("verified", args[0])))
        bind("storage_health", lambda version, dashboards: self.seen.append(("trace", (version, dashboards))))
        bind("patch_state", self.patch)
        self.stack.enter_context(patch.object(a.stack_health, "check", side_effect=self.health))
        self.files = a.prepare_inputs(SLOT, COMMIT, a.ROOT, self.charts, self.image_map)
        self.seen.clear()

    def history(self, key):
        ns, name, _ = a.RELEASES[key]
        release = {"name": name, "namespace": ns, "manifest": json.dumps(obj("ConfigMap", name)),
                   "chart": {"metadata": {"name": a.CHARTS[key], "version": "3.7.0", "appVersion": "3.7.1"}}, "hooks": [], "config": {"uiconfig": '{"tracking":{}}'}}
        return [{"revision": self.versions[key], "uid": "fixture", "status": "deployed", "sha256": "d" * 64}], release, release

    def transaction(self, key, chart, values, *, expected=None, dry=True, atomic=False):
        assert chart == self.charts["/".join(a.RELEASES[key][:2])]
        assert json.loads(values) == a.values_for(key, a.ROOT, self.image_map)
        self.seen.append(("dry" if dry else "upgrade", key))
        if not dry:
            assert expected == a.release_docs(self.history(key)[1])
            if self.fail_writes == key:
                raise a.AdapterError("synthetic failure")
            self.versions[key] += 1
        return self.history(key)[1]

    def baseline(self, key, current, rollback, candidate, extras, selected_images):
        return {a.identity(o, a.RELEASES[key][0]): {"uid": "fixture", "state": self.state[key]} for o in candidate + extras}

    def health(self, stack, *, tracking):
        if self.fail_health_when and self.fail_health_when in self.seen:
            raise a.AdapterError("unhealthy")
        self.seen.append(("health", stack))
        return True

    def patch(self, key, wanted, expected, assert_locked):
        assert expected == ("state-uid", self.checks)
        self.checks[key] = wanted
        self.seen.append(("checkpoint", key))

    def lock(self):
        if self.lost_lock:
            raise a.AdapterError("lost lock")
        self.seen.append(("lock", True))

    def apply(self):
        return a.apply(self.files, slot=SLOT, configuration_sha=COMMIT, assert_locked=self.lock)


class AdapterTests(unittest.TestCase):
    def test_full_plan_and_apply_use_all_six_frozen_releases_and_five_checkpoints(self):
        c = SimulatedCluster(self)
        self.assertEqual(a.MEMBERS, c.files.keys())
        self.assertTrue(c.apply())
        self.assertEqual([k for category, k in c.seen if category == "upgrade"], list(a.RELEASES))
        self.assertEqual([k for category, k in c.seen if category == "checkpoint"], list(a.COMPONENTS))
        self.assertLess(c.seen.index(("verified", "oci-jaeger")), c.seen.index(("upgrade", "opensearch")))
        self.assertLess(c.seen.index(("trace", ("3.7.1", "3.7.1"))), c.seen.index(("checkpoint", "opensearch")))
        self.assertEqual(c.checks, c.sha)

    def test_unchanged_chart_components_leave_only_the_two_daily_snapshots(self):
        c = SimulatedCluster(self, {k: a.deploy.fingerprint(k) for k in a.COMPONENTS})
        self.assertTrue(c.apply())
        self.assertEqual([k for action, k in c.seen if action == "upgrade"], ["oci-jaeger", "otel-jaeger"])
        self.assertLess(c.seen.index(("patch-access", "jaeger")), c.seen.index(("upgrade", "oci-jaeger")))

    def test_snapshot_rbac_preflight_names_every_object_and_requires_a_conclusive_allow(self):
        objects = [obj("Service", "jaeger-query"),
                   dict(obj("Deployment", "jaeger"), apiVersion="apps/v1")]
        with patch.object(a, "kube", return_value=b'{"status":{"allowed":true}}') as request:
            a.check_jaeger_patch_access("jaeger", objects)
            attrs = [json.loads(call.kwargs["body"])["spec"]["resourceAttributes"] for call in request.call_args_list]
            self.assertEqual(attrs, [
                dict(namespace="jaeger", verb="patch", group="apps", resource="deployments", name="jaeger"),
                dict(namespace="jaeger", verb="patch", group="", resource="services", name="jaeger-query")])
        for status in ({"allowed": False}, {"allowed": True, "evaluationError": "timeout"}, {"allowed": True, "denied": True}):
            with self.subTest(status=status), patch.object(a, "kube", return_value=a.encode({"status": status})):
                with self.assertRaisesRegex(a.AdapterError, "lacks reviewed PATCH"):
                    a.check_jaeger_patch_access("jaeger", objects)
        with self.assertRaisesRegex(a.AdapterError, "Unreviewed"):
            a.check_jaeger_patch_access("jaeger", [obj("Secret", "private")])

    def test_live_rbac_comparison_accepts_persisted_empty_rules_only(self):
        a.check_live_rbac({"rules": []}, {"rules": None})
        for before, after in (({"rules": []}, {"rules": [{"verbs": ["get"]}]}),
                              ({"rules": [{"verbs": ["get"]}]}, {"rules": None}),
                              ({"subjects": [{"name": "jaeger"}]}, {"subjects": []}),
                              ({"roleRef": {"name":"original"}}, {"roleRef":{"name":"changed"}}),
                              ({"rules": []}, {"rules": ""})):
            with self.subTest(before=before, after=after), self.assertRaisesRegex(a.AdapterError, "Live RBAC"):
                a.check_live_rbac(before, after)

    def test_monitoring_workloads_require_all_replicas_and_current_generation_conditions(self):
        cr = {"spec": {"replicas": 2, "shards": 2}, "metadata": {"generation": 3},
              "status": {"replicas": 4, "updatedReplicas": 4, "availableReplicas": 4,
                         "conditions": [{"type": kind, "status": "True", "observedGeneration": 3}
                                        for kind in ("Reconciled", "Available")]}}
        self.assertTrue(a.monitoring_workload_current(cr))
        for field, value in (("conditions", []), ("availableReplicas", 3), ("unavailableReplicas", 1),
                             ("updatedReplicas", 3), ("paused", True)):
            with self.subTest(field=field):
                self.assertFalse(a.monitoring_workload_current(dict(cr, status=dict(cr["status"], **{field: value}))))
        with patch.object(a, "get_object", return_value=cr) as read:
            a.check_monitoring_workloads(wait=False)
        self.assertEqual([x.args[0] for x in read.call_args_list], [
            "monitoring.coreos.com/v1:Alertmanager:default:prometheus-kube-prometheus-alertmanager",
            "monitoring.coreos.com/v1:Prometheus:default:prometheus-kube-prometheus-prometheus"])
        cr["status"]["conditions"][0]["observedGeneration"] = 2
        with patch.object(a, "get_object", return_value=cr):
            with self.assertRaisesRegex(a.AdapterError, "current generation"):
                a.check_monitoring_workloads(wait=False)
            with patch.object(a.time, "monotonic", side_effect=[1, 400]), self.assertRaises(a.AdapterError):
                a.check_monitoring_workloads(wait=True)

    def test_inherits_recorded_apply_method_and_rejects_drift_before_any_write(self):
        for previous, expected in (({}, "csa"), ({"apply_method": ""}, "csa"),
                                   ({"apply_method": "csa"}, "csa"), ({"apply_method": "ssa"}, "ssa")):
            self.assertEqual(a.apply_method(previous), expected)
        with self.assertRaisesRegex(a.AdapterError, "Unknown Helm"):
            a.apply_method({"apply_method": "unreviewed"})
        c = SimulatedCluster(self)
        plan = json.loads(c.files["plan.json"])
        self.assertEqual({row["applyMethod"] for row in plan["releases"].values()}, {"csa"})
        plan["releases"]["otel-jaeger"]["applyMethod"] = "ssa"
        c.files["plan.json"] = a.encode(plan)
        with self.assertRaisesRegex(a.AdapterError, "apply method"):
            c.apply()
        self.assertNotIn("upgrade", dict(c.seen))

    def test_late_release_drift_and_replaced_pvc_prevent_the_first_write(self):
        c = SimulatedCluster(self)
        c.state["otel-demo"] = "new"
        with self.assertRaisesRegex(a.AdapterError, "drifted"):
            c.apply()
        self.assertNotIn("upgrade", dict(c.seen))
        c.state["otel-demo"] = "original"
        c.pvc[-1] = "other-volume"
        with self.assertRaisesRegex(a.AdapterError, "PVC"):
            c.apply()
        self.assertNotIn("upgrade", dict(c.seen))

    def test_failed_storage_does_not_checkpoint_or_trigger_otel_jaeger(self):
        c = SimulatedCluster(self)
        c.fail_writes = "opensearch-dashboards"
        with self.assertRaisesRegex(a.AdapterError, "synthetic"):
            c.apply()
        self.assertIn(("upgrade", "opensearch"), c.seen)
        self.assertNotIn(("checkpoint", "opensearch"), c.seen)
        self.assertNotIn(("upgrade", "otel-jaeger"), c.seen)

    def test_health_and_lost_lock_stop_later_work(self):
        c = SimulatedCluster(self)
        c.lost_lock = True
        with self.assertRaisesRegex(a.AdapterError, "lock"):
            c.apply()
        self.assertFalse(c.seen)
        c.lost_lock = False
        c.fail_health_when = ("verified", "oci-jaeger")
        with self.assertRaisesRegex(a.AdapterError, "unhealthy"):
            c.apply()
        self.assertNotIn(("upgrade", "opensearch"), c.seen)
        self.assertNotIn(("checkpoint", "oci-jaeger"), c.seen)

    def test_receipt_identity_members_selection_and_chart_tampering_are_rejected(self):
        c = SimulatedCluster(self)
        for name, value in (("slot", SLOT + "bad"), ("configurationSHA", "b" * 40), ("selected", ["opensearch"])):
            plan = json.loads(c.files["plan.json"])
            plan[name] = value
            with self.assertRaises(a.AdapterError):
                a.validate_plan(dict(c.files, **{"plan.json": a.encode(plan)}), SLOT, COMMIT)
        for changed in (dict(c.files, **{"charts/oci-jaeger.tgz": b"changed"}), dict(c.files, extra=b"executable")):
            with self.assertRaises(a.AdapterError):
                a.validate_plan(changed, SLOT, COMMIT)

    def test_chart_inputs_and_all_twelve_supplemental_objects(self):
        imgs = images()
        for k, directory in (("oci-jaeger", "oci"), ("otel-jaeger", "otel-demo")):
            values = a.values_for(k, a.ROOT, imgs)
            self.assertEqual(values["hotrod"]["image"]["tag"], imgs["hotrod"]["tag"] + "@" + imgs["hotrod"]["digest"])
            self.assertEqual(values["allInOne"]["image"]["tag"], imgs["jaeger"]["tag"] + "@" + imgs["jaeger"]["digest"])
            self.assertEqual(values["storage"]["type"], "memory" if directory == "oci" else "elasticsearch")
        resources = {key: a.supplemental_for(key, a.ROOT) for key in a.COMPONENTS}
        self.assertEqual(sum(map(len, resources.values())), 12)
        for key, supplemental in resources.items():
            self.assertEqual(len(a.inventory(key, [], supplemental)), len(supplemental))

    def test_new_resource_privileged_changes_and_unexpected_storage_topology_fail_closed(self):
        ns = "opensearch"
        role = {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": "x"}, "rules": []}
        changed = dict(role, rules=[{"verbs": ["*"]}])
        with self.assertRaisesRegex(a.AdapterError, "Privileged"):
            a.validate_change(ns, [role], [changed], images())
        with self.assertRaisesRegex(a.AdapterError, "inventory"):
            a.validate_change(ns, [], [role], images())
        state = {"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"name": "db"},
                 "spec": {"replicas": 1, "volumeClaimTemplates": []}}
        for spec in (dict(state["spec"], replicas=0), dict(state["spec"], volumeClaimTemplates=[{}])):
            with self.assertRaises(a.AdapterError):
                a.validate_change(ns, [state], [dict(state, spec=spec)], images())
        with self.assertRaisesRegex(a.AdapterError, "inventory"):
            a.inventory("opensearch-dashboards", [state], [])

    def test_first_tag_only_promotion_fails_without_automatic_rollback_or_checkpoint(self):
        c = SimulatedCluster(self)
        c.fail_writes = "oci-jaeger"
        with self.assertRaisesRegex(a.AdapterError, "partial changes may remain"):
            c.apply()
        self.assertIn(("upgrade", "oci-jaeger"), c.seen)
        self.assertNotIn(("checkpoint", "oci-jaeger"), c.seen)
        self.assertNotIn(("upgrade", "opensearch"), c.seen)

    def test_rollback_is_atomic_only_when_both_ready_prior_images_are_digest_pinned(self):
        import yaml
        refs = {r: s["reference"] for r, s in images().items()}
        sources = images()
        def verify():
            docs, evidence = [], {}
            for role, name in (("jaeger", "jaeger"), ("hotrod", "jaeger-hotrod")):
                docs.append({"apiVersion":"apps/v1", "kind":"Deployment","metadata":{"name":name},
                             "spec":{"template":{"spec":{"containers":[{"name":name,"image":refs[role]}]}}}})
                evidence[role] = {"reference":refs[role], "runtimeDigests":[sources[role]["platformManifest"]]}
            release = {"manifest": yaml.safe_dump_all(docs), "hooks":[]}
            with patch.object(a, "snapshot_pods", return_value=evidence):
                return a.rollback_policy("oci-jaeger", release)
        self.assertTrue(verify()[0])
        refs["hotrod"] = refs["hotrod"].split("@")[0]
        self.assertFalse(verify()[0])
        refs["jaeger"] = refs["jaeger"].split("@")[0]
        self.assertFalse(verify()[0])

    def test_verified_atomic_failure_requires_original_running_digests(self):
        c = SimulatedCluster(self)
        c.fail_writes = "oci-jaeger"
        plan = json.loads(c.files["plan.json"])
        row = plan["releases"]["oci-jaeger"]
        row["atomicRollback"] = True
        row["priorSnapshotPods"] = {"jaeger":{"reference":"pinned", "runtimeDigests":["exact"]}}
        c.files["plan.json"] = a.encode(plan)
        original = c.history
        def history(key):
            records, current, fallback = original(key)
            if key == "oci-jaeger" and ("upgrade", key) in c.seen:
                records[0]["revision"] += 2
            return records, current, fallback
        with patch.object(a, "history", side_effect=history), \
             patch.object(a, "rollback_policy", side_effect=lambda k, _: (True, row["priorSnapshotPods"]) if k == "oci-jaeger" else (False, {})), \
             patch.object(a, "snapshot_pods", return_value={"other":"image"}):
            with self.assertRaisesRegex(a.AdapterError, "rollback remains unverified"):
                c.apply()
        self.assertNotIn(("checkpoint", "oci-jaeger"), c.seen)

    def test_only_the_slot_restart_changes_and_unchanged_supplements_skip_patch(self):
        desired = next(o for o in a.supplemental_for("oci-jaeger", a.ROOT) if o["kind"] == "Deployment")
        prior = json.loads(a.encode(desired))
        prior["metadata"].pop("namespace", None)
        prior["metadata"]["creationTimestamp"] = None
        live = json.loads(a.encode(desired))
        live["metadata"]["creationTimestamp"] = "2026-11-01T00:00:00Z"
        live["metadata"].setdefault("annotations", {})[a.LAST_APPLIED] = json.dumps(prior, indent=2)
        live["spec"]["template"]["metadata"].setdefault("annotations", {})[a.RESTARTED] = "yesterday"
        desired["spec"]["template"]["metadata"].setdefault("annotations", {})[a.RESTARTED] = SLOT
        self.assertEqual(a.supplemental_changes(desired, live), [
            {"op":"add","path":"/spec/template/metadata/annotations/kubectl.kubernetes.io~1restartedAt","value":SLOT}])
        live["spec"]["template"]["metadata"]["annotations"][a.RESTARTED] = SLOT
        self.assertEqual(a.supplemental_changes(desired, live), [])
        del live["metadata"]["annotations"][a.LAST_APPLIED]
        with self.assertRaisesRegex(a.AdapterError, "previous declarative"):
            a.supplemental_changes(desired, live)

    def test_real_helm_metadata_normalization_ignores_only_empty_top_level_annotations(self):
        plain = obj("ConfigMap", "x", data={"key": "value"})
        def annotated(value):
            return dict(plain, metadata={"name": "x", "annotations": value})
        for value in (None, {}):
            self.assertEqual(a.index([plain], "default"), a.index([annotated(value)], "default"))
        self.assertNotEqual(a.index([plain], "default"), a.index([annotated({"owner": "changed"})], "default"))

    def test_context_must_match_independent_target_cluster_and_pinned_tools(self):
        with patch.object(a, "command", return_value=b"v4.3.2"), patch.object(a, "get_object", return_value={"metadata":{"uid":"right"}}), \
             patch.object(a, "kube", return_value=b'{"gitVersion":"v1.36.1"}'):
            for uid in ("", "wrong"):
                with patch.dict(os.environ, {"SCHEDULED_CLUSTER_UID": uid}), self.assertRaisesRegex(a.AdapterError, "target"):
                    a.check_context()
            with patch.dict(os.environ, {"SCHEDULED_CLUSTER_UID": "right"}):
                self.assertEqual(a.check_context()["clusterUID"], "right")

    def test_command_bounds_memory_time_and_failure_diagnostics(self):
        with patch.object(a, "LIMIT", 100), self.assertRaisesRegex(a.AdapterError, "Oversized"):
            a.command([sys.executable, "-c", "print('private' * 10000)"])
        with self.assertRaisesRegex(a.AdapterError, "timed out"):
            a.command([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.1)
        payload = b"x" * 200000
        self.assertEqual(a.command([sys.executable, "-c", "import sys; print(len(sys.stdin.buffer.read()))"], body=payload),
                         b"200000\n")

    def test_readiness_binds_registry_or_platform_digest_and_pod_owner(self):
        sources = images()
        selected = {"jaeger": sources["jaeger"], "jaeger-hotrod": sources["hotrod"]}
        for image in selected.values():
            image["platformManifest"] = "sha256:" + "f" * 64
        def get(ident):
            name = ident.split(":")[-1]
            if ":ReplicaSet:" in ident:
                return {"metadata": {"uid": name, "ownerReferences": [{"controller": True, "kind":"Deployment", "apiVersion":"apps/v1",
                        "uid": name.removesuffix("-rs"), "name": name.removesuffix("-rs")}]}}
            return {"metadata":{"generation":1, "uid":name}, "spec":{"replicas":1, "selector":{"matchLabels":{"release":name}},
                    "template":{"spec":{"containers":[{"name":name, "image":selected[name]["reference"]}]}}},
                    "status":{"observedGeneration":1,"replicas":1,"updatedReplicas":1,"availableReplicas":1,"readyReplicas":1}}
        running_digest = "sha256:" + "f" * 64
        def pods(*args):
            name = next(a for a in args if a.startswith("release=")).split("=")[-1]
            pod = {"metadata":{"ownerReferences":[{"controller":True, "kind":"ReplicaSet", "apiVersion":"apps/v1", "uid":name+"-rs", "name":name+"-rs"}]},
                   "spec":{"containers":[{"name":name, "image":selected[name]["reference"]}]},
                   "status":{"phase":"Running", "conditions":[{"type":"Ready","status":"True"}],
                             "containerStatuses":[{"name": name, "ready":True,"imageID":"docker.io/test@"+running_digest}]}}
            return a.encode({"items":[pod]})
        with patch.object(a, "get_object", side_effect=get), patch.object(a, "kube", side_effect=pods):
            a.ready_images("oci-jaeger", sources)
            running_digest = "sha256:" + "e" * 64
            with self.assertRaisesRegex(a.AdapterError, "registry digest"):
                a.ready_images("oci-jaeger", sources)

    def test_patch_drift_and_preserve_defaulted_fields(self):
        old = {"spec": {"ports": [{"port": 80}], "retired": "yes", "marker": "ok"}}
        new = {"spec": {"ports": [{"port": 80}], "marker": "ok"}}
        actual = {"spec": {"retired": "yes", "marker": "drift", "allocated": "keep", "ports": [{"port": 80, "protocol": "TCP"}]}}
        self.assertEqual(a.edits(old, new, actual), [{"op": "remove", "path": "/spec/retired"},
                         {"op": "add", "path": "/spec/marker", "value": "ok"}])


if __name__ == "__main__":
    unittest.main()
