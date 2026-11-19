# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import base64
import copy
import gzip
import unittest
from unittest.mock import Mock

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

    def test_paginated_metadata_rejects_full_secret_list_and_mixed_snapshots(self):
        def page(rv="39", cont=""):
            return {"kind": "PartialObjectMetadataList", "apiVersion": "meta.k8s.io/v1",
                    "items": None, "metadata": {"resourceVersion": rv, "continue": cont}}
        self.assertEqual(o.ledger(Mock(return_value=page()), "default", "prometheus"), {})
        for rows in ([page(cont="a"), page("40")], [page(cont="a"), page(cont="a")],
                     [dict(page(), kind="SecretList")], [dict(page(), items=[{"metadata": {}, "data": {}}])]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                o.ledger(Mock(side_effect=rows), "default", "prometheus")

    def test_record_compares_payload_identity_and_hashes_stored_hooks(self):
        release = {"namespace": "default", "name": "prometheus", "version": 9, "info": {"status": "superseded"},
                   "chart": {"metadata": {"name": "prometheus", "version": "1.0.0"}}, "hooks": [{"manifest": "first"}]}
        labels = {"owner": "helm", "name": "prometheus", "version": "9", "status": "superseded"}
        meta = {"namespace": "default", "name": "sh.helm.release.v1.prometheus.v9", "uid": "uid", "resourceVersion": "12", "labels": labels}
        def read(payload, compress=True):
            raw = o.canonical(payload)
            content = gzip.compress(raw) if compress else raw
            encoded = base64.b64encode(base64.b64encode(content)).decode()
            get = Mock(return_value={"kind": "Secret", "apiVersion": "v1", "type": "helm.sh/release.v1",
                                     "metadata": meta, "data": {"release": encoded}})
            result = o.record(get, "default", "prometheus", 9, ("superseded", "uid", "12"))
            get.assert_called_once_with("/api/v1/namespaces/default/secrets/sh.helm.release.v1.prometheus.v9")
            return result
        initial = read(release)
        self.assertEqual(initial, read(release, False))
        self.assertNotEqual(initial["recordSHA256"], read(dict(release, hooks=[{"manifest": "changed"}]))["recordSHA256"])
        for changed in ({"version": True}, {"info": {"status": "failed"}}, {"name": "other"}, {"namespace": "jaeger"}):
            with self.assertRaisesRegex(ValueError, "payload"):
                read(dict(release, **changed))

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


if __name__ == "__main__":
    unittest.main()
