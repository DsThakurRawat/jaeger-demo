# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import contextlib
import copy
import io
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import live_observation as o


class ReadOnlyTests(unittest.TestCase):
    def test_server_scope_blocks_ambiguous_unknown_namespaced_and_subresource_reads(self):
        endpoints = {
            "/api": {"kind": "APIVersions", "versions": ["v1"]},
            "/apis": {"kind": "APIGroupList", "groups": [{"versions": [{"groupVersion": "apps/v1"}]}]},
            "/version": {"gitVersion": "v1.36.1"},
        }
        def entry(name, kind, namespaced):
            return {"name": name, "kind": kind, "namespaced": namespaced, "verbs": ["get"]}
        for group, entries in (("v1", [entry("namespaces", "Namespace", False), entry("secrets", "Secret", True)]),
                               ("apps/v1", [entry("deployments", "Deployment", True), entry("deployments/scale", "Scale", True),
                                            entry("ambiguous", "Ambiguous", True), entry("other", "Ambiguous", False)])):
            endpoints[("/api/" if group == "v1" else "/apis/") + group] = {"kind": "APIResourceList", "groupVersion": group, "resources": entries}
        routes, caps = o.discover(endpoints.get)
        self.assertIn("apps/v1/Scale", caps["apiVersions"])
        self.assertEqual(o.item_path("v1:Namespace:-:default", routes), "/api/v1/namespaces/default")
        self.assertEqual(o.item_path("apps/v1:Deployment:jaeger:jaeger", routes), "/apis/apps/v1/namespaces/jaeger/deployments/jaeger")
        for bad in ("v1:Secret:-:secret", "v1:Namespace:default:test", "apps/v1:Scale:default:jaeger",
                    "apps/v1:Ambiguous:default:n", "v1:Secret:default:..", "v1:Secret:default:s/other"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                o.item_path(bad, routes)

    def test_paginated_metadata_rejects_other_lists_and_mixed_snapshots(self):
        def page(rv="39", cont="", items=None):
            return {"kind": "PartialObjectMetadataList", "apiVersion": "meta.k8s.io/v1",
                    "items": items, "metadata": {"resourceVersion": rv, "continue": cont}}
        def read(rows):
            get = Mock(side_effect=rows)
            items = list(o.pages(get, "/api/v1/namespaces/default/secrets", "owner=helm,name=prometheus",
                                 o.METADATA, "meta.k8s.io/v1", "PartialObjectMetadataList"))
            for args in get.call_args_list:
                self.assertEqual(args.args[1], o.METADATA)
                self.assertIn("labelSelector=owner%3Dhelm%2Cname%3Dprometheus", args.args[0])
            return items
        self.assertEqual(read([page()]), [])
        self.assertEqual(read([page(cont="a", items=[{"metadata": {"name": "one"}}]),
                               page(items=[{"metadata": {"name": "two"}}])]),
                         [{"metadata": {"name": "one"}}, {"metadata": {"name": "two"}}])
        for rows in ([page(cont="a"), page("40")], [page(cont="a"), page(cont="a")],
                     [dict(page(), kind="SecretList")], [dict(page(), items={"metadata": {}})], [None]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                read(rows)

    def test_runtime_image_requires_ready_replica_and_true_owner_chain(self):
        image = o.REPOS["jaeger"] + ":" + "a" * 40
        spec = {"containers": [{"name": "jaeger", "image": image}]}
        labels = {"component": "jaeger"}
        state = {"observedGeneration": 1, "replicas": 1, "readyReplicas": 1, "availableReplicas": 1, "updatedReplicas": 1}
        def meta(name):
            return {"name": name, "namespace": "default", "uid": name + "-uid", "labels": labels, "generation": 1}
        def owner(name, kind):
            return {"name": name, "kind": kind, "uid": name + "-uid", "apiVersion": "apps/v1", "controller": True}
        parent = {"kind": "Deployment", "apiVersion": "apps/v1", "metadata": meta("jaeger"), "status": state,
                  "spec": {"replicas": 1, "selector": {"matchLabels": labels}, "template": {"spec": spec}}}
        rs = {"kind": "ReplicaSet", "apiVersion": "apps/v1", "metadata": dict(meta("rs"), ownerReferences=[owner("jaeger", "Deployment")])}
        actual = {"name": "jaeger", "ready": True, "imageID": "docker.io/" + image + "@sha256:" + "b" * 64}
        pod = {"kind": "Pod", "apiVersion": "v1", "metadata": dict(meta("pod"), ownerReferences=[owner("rs", "ReplicaSet")]), "spec": spec,
               "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}], "containerStatuses": [actual]}}
        pods = {"kind": "PodList", "apiVersion": "v1", "metadata": {"resourceVersion": "10"}, "items": [pod]}
        src = {"digest": "sha256:" + "c" * 64, "platformManifest": "sha256:" + "b" * 64, "sourceCommit": "a" * 40}
        def read(d=parent, p=pods, r=rs):
            return o.live_image(Mock(side_effect=[d, p, r]), Mock(return_value=src), "default", "jaeger")
        self.assertEqual(read(), {"digest": src["digest"], "sourceCommit": src["sourceCommit"], "reference": image})
        del pod["kind"], pod["apiVersion"]
        self.assertEqual(read()["digest"], src["digest"])
        with self.assertRaisesRegex(ValueError, "selected Pod"):
            read(p=dict(pods, items=[dict(pod, kind="Secret")]))
        with self.assertRaisesRegex(ValueError, "all Deployment replicas"):
            read(p=dict(pods, items=[]))
        with self.assertRaisesRegex(ValueError, "reconciling"):
            read(d=dict(parent, status=dict(state, observedGeneration=0)))
        bad = copy.deepcopy(rs)
        bad["metadata"]["ownerReferences"][0]["uid"] = "other"
        with self.assertRaisesRegex(ValueError, "owner chain"):
            read(r=bad)
        actual["imageID"] = "docker.io/" + image + "@sha256:" + "d" * 64
        with self.assertRaisesRegex(ValueError, "Running Pod digest"):
            read()


    def test_snapshot_fingerprints_named_objects_and_reads_each_stack_component(self):
        deployment = {"apiVersion": "apps/v1", "kind": "Deployment", "status": {"replicas": 1},
                      "metadata": {"name": "jaeger", "namespace": "default", "uid": "u1", "resourceVersion": "5"},
                      "spec": {"replicas": 1}}
        routes = {("apps/v1", "Deployment"): ("/apis/apps/v1", "deployments", True)}
        paths = {"/apis/apps/v1/namespaces/default/deployments/jaeger": deployment}
        images = {"reference": "r", "sourceCommit": "c", "digest": "d"}
        with (patch.object(o, "discover", return_value=(routes, {})) as discover,
              patch.object(o, "live_image", return_value=images) as live_image):
            result = o.snapshot(paths.get, ["apps/v1:Deployment:default:jaeger", "apps/v1:Deployment:default:gone"], ["otel"])
            self.assertEqual(result["objects"]["apps/v1:Deployment:default:gone"], None)
            fingerprint = result["objects"]["apps/v1:Deployment:default:jaeger"]
            self.assertEqual(fingerprint["uid"], "u1")
            # Status and transient metadata do not change the fingerprint; the spec does.
            deployment["status"] = {"replicas": 0}
            deployment["metadata"]["resourceVersion"] = "6"
            self.assertEqual(o.snapshot(paths.get, ["apps/v1:Deployment:default:jaeger"], [])["objects"]
                             ["apps/v1:Deployment:default:jaeger"], fingerprint)
            deployment["spec"]["replicas"] = 2
            self.assertNotEqual(o.snapshot(paths.get, ["apps/v1:Deployment:default:jaeger"], [])["objects"]
                                ["apps/v1:Deployment:default:jaeger"], fingerprint)
            self.assertEqual(result["images"], {"otel": {"jaeger": images, "hotrod": images}})
            self.assertEqual(sorted(c.args[2:] for c in live_image.call_args_list[:2]), [("jaeger", "hotrod"), ("jaeger", "jaeger")])
            discover.reset_mock()
            self.assertEqual(o.snapshot(paths.get, [], ["oci"])["objects"], {})
            discover.assert_not_called()
            for objects, stacks in (([], []), ([], ["other"]), ([], ["oci", "oci"]), (["v1:Namespace:-:a"] * 2, [])):
                with self.subTest(objects=objects, stacks=stacks), self.assertRaisesRegex(ValueError, "read scope"):
                    o.snapshot(paths.get, objects, stacks)

    def test_cli_prints_only_the_snapshot_and_hides_failure_details(self):
        @contextlib.contextmanager
        def reader(kubeconfig, context):
            self.assertEqual((kubeconfig, context), ("cfg", "ctx"))
            yield "get"
        args = ["--kubeconfig", "cfg", "--context", "ctx", "--stack", "oci"]
        out = io.StringIO()
        with (patch.object(o, "reader", reader), patch.object(o, "snapshot", return_value={"images": {}}) as snap,
              contextlib.redirect_stdout(out)):
            o.main(args)
        snap.assert_called_once_with("get", [], ["oci"])
        self.assertEqual(out.getvalue(), '{"images":{}}\n')
        err = io.StringIO()
        with (patch.object(o, "reader", reader), patch.object(o, "snapshot", side_effect=KeyError("private-value")),
              contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as exit):
            o.main(args)
        self.assertEqual(exit.exception.code, 1)
        self.assertNotIn("private-value", err.getvalue())
        self.assertIn("KeyError", err.getvalue())
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as exit:
            o.main(["--kubeconfig", "cfg", "--context", "ctx"])
        self.assertEqual(exit.exception.code, 2)

    def test_module_is_independent_of_the_bundle_contract(self):
        source = Path(o.__file__).read_text()
        self.assertNotIn("adoption_bundle", source)
        for removed in ("observe", "live_reader", "history", "ledger", "record", "functional_health"):
            self.assertFalse(hasattr(o, removed), removed)
        with self.assertRaisesRegex(ValueError, "Duplicate JSON property"):
            o.read_json(b'{"a":1,"a":2}')
        with self.assertRaisesRegex(ValueError, "Non-finite"):
            o.read_json(b'{"a":NaN}')
        self.assertEqual(o.canonical({"b": 1, "a": [1]}), b'{"a":[1],"b":1}')

if __name__ == "__main__":
    unittest.main()
