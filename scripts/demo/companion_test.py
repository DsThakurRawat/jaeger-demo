# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch

import yaml

import companion_ownership as ownership
from companion_values import GENERATORS, ROOT, source_values


class CompanionTest(unittest.TestCase):
    @staticmethod
    def render(namespace, values):
        return subprocess.run(
            ["helm", "template", "demo-resources", str(ROOT / "charts/demo-resources"),
             "--namespace", namespace, "--values", "-"],
            input=json.dumps(values), text=True, capture_output=True,
        )

    def test_all_named_resources_render_legacy_inputs_with_keep(self):
        inputs = source_values()
        expected_counts = {"default": 6, "jaeger": 4, "opensearch": 1, "otel-demo": 1}
        self.assertEqual(expected_counts.keys(), inputs["companions"].keys())
        for ns, values in inputs["companions"].items():
            with self.subTest(namespace=ns):
                result = self.render(ns, values)
                self.assertEqual(result.returncode, 0, result.stderr)
                outputs = list(yaml.safe_load_all(result.stdout))
                self.assertEqual(len(outputs), expected_counts[ns])
                wanted = {(o["kind"], o["metadata"]["name"]): deepcopy(o) for o in values["objects"]}
                for obj in outputs:
                    meta = obj["metadata"]
                    self.assertEqual(meta.pop("namespace"), ns)
                    self.assertEqual(meta["annotations"].pop("helm.sh/resource-policy"), "keep")
                    if not meta["annotations"]:
                        del meta["annotations"]
                    original = wanted.pop((obj["kind"], meta["name"]))
                    original["metadata"].pop("namespace", None)
                    self.assertEqual(obj, original)
                    if obj["kind"] == "ConfigMap":
                        for path in GENERATORS[ns]:
                            self.assertEqual(obj["data"][path.split("/")[-1]].encode(), (ROOT / "examples" / path).read_bytes())
                self.assertFalse(wanted)

    def test_chart_fails_closed_on_unknown_missing_duplicate_and_foreign_objects(self):
        objects = source_values()["companions"]["opensearch"]["objects"]
        mutations = [
            ([], "missing resource"),
            (objects + objects, "duplicate resource"),
            ([dict(objects[0], kind="ClusterIssuer")], "unexpected resource"),
            ([dict(objects[0], metadata={"name": "unknown"})], "unexpected resource"),
            ([dict(objects[0], metadata=dict(objects[0]["metadata"], namespace="default"))], "foreign namespace"),
            ([dict(objects[0], apiVersion="v1")], "unexpected API version"),
            ([dict(objects[0], metadata=dict(objects[0]["metadata"], annotations={"helm.sh/hook": "pre-upgrade"}))],
             "hook annotation"),
            ([dict(objects[0], metadata=dict(objects[0]["metadata"],
                   annotations={"helm.sh/hook-delete-policy": "before-hook-creation"}))], "hook annotation"),
        ]
        for changed, error in mutations:
            with self.subTest(error=error):
                result = self.render("opensearch", {"objects": changed})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(error, result.stderr)
        self.assertIn("unsupported demo namespace", self.render("kube-system", {"objects": []}).stderr)

    def test_ownership_plan_uses_named_reads_and_only_changes_metadata(self):
        seen = []
        def read(cmd, **kwargs):
            ns, kind, name = cmd[cmd.index("--namespace")+1], cmd[-3], cmd[-2]
            self.assertEqual(cmd, ["kubectl", "--kubeconfig", "fixture", "--context", "fixture",
                                   "--namespace", ns, "get", kind, name, "--output=json"])
            seen.append((ns, kind, name))
            value = {"apiVersion": ownership.API[kind], "kind": kind, "metadata": {
                "namespace": ns, "name": name, "uid": "known-uid", "resourceVersion": "9",
                "labels": {"existing": "retain"}, "annotations": {"custom": "retain"}}}
            return subprocess.CompletedProcess(cmd, 0, json.dumps(value))
        with patch.object(ownership.subprocess, "run", side_effect=read):
            plans = ownership.plan("fixture", "fixture")
        self.assertEqual(len(set(seen)), 12)
        for item in plans:
            commands = item["patch"]
            self.assertEqual(commands[:2], [
                {"op": "test", "path": "/metadata/uid", "value": "known-uid"},
                {"op": "test", "path": "/metadata/resourceVersion", "value": "9"},
            ])
            self.assertEqual({op["path"] for op in commands[2:]}, {
                "/metadata/labels/app.kubernetes.io~1managed-by", "/metadata/annotations/meta.helm.sh~1release-name",
                "/metadata/annotations/meta.helm.sh~1release-namespace", "/metadata/annotations/helm.sh~1resource-policy"})
            self.assertTrue(all(op["op"] == "add" for op in commands[2:]))

    def test_owned_object_is_idempotent_and_conflicts_abort(self):
        meta = {"name": "prometheus", "namespace": "default", "uid": "id", "resourceVersion": "7"}
        obj = {"kind": "Service", "apiVersion": "v1", "metadata": meta}
        def check():
            return ownership.ownership_patch("default", "Service", "prometheus", obj)
        for element in check()[2:]:
            meta[element["path"].split("/")[-1]] = element["value"]
        self.assertEqual(check(), [])
        for key, update in [
                ("labels", {"app.kubernetes.io/managed-by": "other"}),
                ("annotations", {"meta.helm.sh/release-name": "foreign"}),
                ("annotations", {"meta.helm.sh/release-name": "demo-resources"}),
                ("annotations", {**meta["annotations"], "helm.sh/resource-policy": "delete"}),
                ("annotations", {**meta["annotations"], "meta.helm.sh/release-namespace": "jaeger"}),
                ("ownerReferences", [{"name": "some-controller"}]), ("deletionTimestamp", "2026-11-18"),
                ("uid", ""), ("resourceVersion", ""), ("namespace", "other"), ("name", "unknown")]:
            with self.subTest(key=key, update=update), patch.dict(meta, {key: update}), self.assertRaises(ValueError):
                check()

    @staticmethod
    def apply_patch(obj, operations):
        """Apply the metadata-only JSON Patch subset these plans use."""
        for operation in operations:
            parts = [part.replace("~1", "/").replace("~0", "~") for part in operation["path"].split("/")[1:]]
            parent = obj
            for part in parts[:-1]:
                parent = parent[part]
            if operation["op"] == "test":
                assert parent[parts[-1]] == operation["value"], operation
            elif operation["op"] == "add":
                parent[parts[-1]] = deepcopy(operation["value"])
            else:
                del parent[parts[-1]]

    def test_de_adoption_removes_exactly_the_recorded_metadata(self):
        for original in ({}, {"labels": {"existing": "retain"}, "annotations": {"custom": "retain"}},
                         {"labels": {"existing": "retain"}}, {"annotations": {"helm.sh/resource-policy": "keep"}}):
            with self.subTest(original=original):
                meta = {"name": "prometheus", "namespace": "default", "uid": "id", "resourceVersion": "7", **deepcopy(original)}
                obj = {"kind": "Service", "apiVersion": "v1", "metadata": meta, "spec": {"clusterIP": "10.0.0.7"}}
                before = deepcopy(obj)
                recorded = ownership.ownership_patch("default", "Service", "prometheus", obj)
                self.apply_patch(obj, recorded)
                meta["resourceVersion"] = "8"
                meta["labels"]["added-later"] = "retain"
                release = ownership.release_patch("default", "Service", "prometheus", obj, recorded)
                self.assertEqual(release[:2], [{"op": "test", "path": "/metadata/uid", "value": "id"},
                                               {"op": "test", "path": "/metadata/resourceVersion", "value": "8"}])
                self.assertEqual({op["op"] for op in release[2:]}, {"test", "remove"})
                self.apply_patch(obj, release)
                # Only what adoption added is gone. Later and earlier metadata, the spec and the UID stay.
                expected = deepcopy(before)
                expected["metadata"]["resourceVersion"] = "8"
                expected["metadata"].setdefault("labels", {})["added-later"] = "retain"
                expected["metadata"].setdefault("annotations", {})
                self.assertEqual(expected, obj)
                self.assertEqual([], ownership.release_patch("default", "Service", "prometheus", obj, recorded))

    def test_de_adoption_refuses_another_object_changed_metadata_and_foreign_records(self):
        meta = {"name": "prometheus", "namespace": "default", "uid": "id", "resourceVersion": "7"}
        obj = {"kind": "Service", "apiVersion": "v1", "metadata": meta}
        recorded = ownership.ownership_patch("default", "Service", "prometheus", obj)
        self.apply_patch(obj, recorded)
        def release(record=recorded):
            return ownership.release_patch("default", "Service", "prometheus", obj, record)
        self.assertTrue(release())
        for key, update in [("uid", "replacement"), ("ownerReferences", [{"name": "controller"}]),
                            ("deletionTimestamp", "2026-11-21"), ("namespace", "other"),
                            ("annotations", {**meta["annotations"], "meta.helm.sh/release-name": "another"}),
                            ("labels", {"app.kubernetes.io/managed-by": "kubectl"})]:
            with self.subTest(key=key), patch.dict(meta, {key: update}), self.assertRaises(ValueError):
                release()
        for foreign in ([{"op": "remove", "path": "/metadata/labels/team"}],
                        [{"op": "add", "path": "/spec/type", "value": "LoadBalancer"}],
                        [{"op": "add", "path": "/metadata/labels/team", "value": "demo"}],
                        [{"op": "add", "path": "/metadata/annotations", "value": {"meta.helm.sh/release-name": "another"}}],
                        [{"op": "add", "path": "/metadata/finalizers", "value": []}]):
            with self.subTest(record=foreign), self.assertRaisesRegex(ValueError, "Unexpected recorded"):
                release(recorded[:2] + foreign)

    def test_de_adoption_plan_uses_named_reads_within_the_allowlist(self):
        def live(ns, kind, name, owned):
            meta = {"namespace": ns, "name": name, "uid": "known-uid", "resourceVersion": "9"}
            obj = {"apiVersion": ownership.API[kind], "kind": kind, "metadata": meta}
            if owned:
                self.apply_patch(obj, ownership.ownership_patch(ns, kind, name, deepcopy(obj)))
            return obj
        owned, seen = False, []
        def read(cmd, **kwargs):
            ns, kind, name = cmd[cmd.index("--namespace")+1], cmd[-3], cmd[-2]
            self.assertEqual(cmd, ["kubectl", "--kubeconfig", "fixture", "--context", "fixture",
                                   "--namespace", ns, "get", kind, name, "--output=json"])
            seen.append((ns, kind, name))
            return subprocess.CompletedProcess(cmd, 0, json.dumps(live(ns, kind, name, owned)))
        with tempfile.TemporaryDirectory() as temp, patch.object(ownership.subprocess, "run", side_effect=read):
            record = Path(temp) / "ownership.json"
            plans = ownership.plan("fixture", "fixture")
            record.write_text(json.dumps(plans))
            owned = True
            seen.clear()
            release = ownership.release_plan("fixture", "fixture", record)
            self.assertEqual((12, 12), (len(release), len(set(seen))))
            self.assertTrue(all(item["patch"] for item in release))
            seen.clear()
            scoped = ownership.release_plan("fixture", "fixture", record, "opensearch")
            self.assertEqual([("opensearch", "Ingress", "opensearch-demo-ingress")], seen)
            self.assertEqual(1, len(scoped))
            seen.clear()
            for changed in (plans + plans[:1], plans + [dict(plans[0], name="kubernetes")],
                            [dict(plans[0], namespace="kube-system")], [dict(plans[0], kind="Secret")]):
                record.write_text(json.dumps(changed))
                with self.assertRaisesRegex(ValueError, "allowlist"):
                    ownership.release_plan("fixture", "fixture", record, "kube-node-lease")
            self.assertEqual([], seen)

    def run_apply_script(self, plan, kubectl_exit=0):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            calls = root / "calls"
            (root / "bin").mkdir()
            fake = root / "bin" / "kubectl"
            fake.write_text('#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$CALLS"\nexit "$KUBECTL_EXIT"\n')
            fake.chmod(0o755)
            (root / "plan.json").write_text(json.dumps(plan))
            result = subprocess.run(["bash", str(ROOT / "scripts/demo/apply-ownership-patches.sh"), str(root / "plan.json"),
                                     "kubeconfig.yaml", "fixture"], capture_output=True, text=True,
                                    env=dict(os.environ, PATH=str(root / "bin") + os.pathsep + os.environ["PATH"],
                                             CALLS=str(calls), KUBECTL_EXIT=str(kubectl_exit)))
            return result, calls.read_text().splitlines() if calls.exists() else []

    def test_apply_script_patches_only_guarded_metadata_and_stops_on_failure(self):
        guards = [{"op": "test", "path": "/metadata/uid", "value": "id"}, {"op": "test", "path": "/metadata/resourceVersion", "value": "7"}]
        add = {"op": "add", "path": "/metadata/annotations/helm.sh~1resource-policy", "value": "keep"}
        item = {"namespace": "default", "kind": "Service", "name": "prometheus", "patch": guards + [add]}
        other = {"namespace": "jaeger", "kind": "Ingress", "name": "jaeger-demo-ingress", "patch": guards + [
            {"op": "test", "path": "/metadata/labels/app.kubernetes.io~1managed-by", "value": "Helm"},
            {"op": "remove", "path": "/metadata/labels/app.kubernetes.io~1managed-by"}]}
        unchanged = {"namespace": "default", "kind": "ConfigMap", "name": "trace-script", "patch": []}
        result, calls = self.run_apply_script([item, unchanged, other])
        self.assertEqual((0, "Applied 2 metadata patches."), (result.returncode, result.stdout.strip()))
        self.assertEqual(2, len(calls))
        self.assertTrue(calls[0].startswith("--kubeconfig kubeconfig.yaml --context fixture --namespace default patch Service prometheus --type=json --patch "))
        self.assertEqual(guards + [add], json.loads(calls[0].split("--patch ", 1)[1]))
        self.assertIn("--namespace jaeger patch Ingress jaeger-demo-ingress", calls[1])
        result, calls = self.run_apply_script([item, other], kubectl_exit=1)
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(1, len(calls))
        for bad in ([dict(item, patch=guards + [{"op": "add", "path": "/spec/type", "value": "LoadBalancer"}])],
                    [dict(item, patch=guards + [{"op": "replace", "path": "/metadata/labels/team", "value": "x"}])],
                    [dict(item, patch=guards + [{"op": "remove", "path": "/metadata/finalizers"}])],
                    [dict(item, patch=guards + [{"op": "add", "path": "/metadata/labels/a/b", "value": "x"}])],
                    [dict(item, patch=[add])], [dict(item, patch=guards[:1] + [add])],
                    [dict(item, kind="Secret")], [dict(item, kind="Namespace")], {"not": "a list"}):
            with self.subTest(plan=bad):
                result, calls = self.run_apply_script(bad)
                self.assertEqual((1, []), (result.returncode, calls))
                self.assertIn("Unexpected patch plan", result.stderr)
