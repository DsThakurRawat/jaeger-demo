# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import json
import subprocess
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
