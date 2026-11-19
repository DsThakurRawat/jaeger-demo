# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import base64
import bz2
import unittest

import yaml

from adoption_bundle import BundleError, digest, transaction_render
from adoption_bundle_test import chart


def resource(name):
    return {"apiVersion":"apiextensions.k8s.io/v1", "kind":"CustomResourceDefinition",
            "metadata":{"name":name}, "spec":{"group":"example.org"}}


def document(source, obj):
    return f"---\n# Source: {source}\n".encode() + yaml.safe_dump(obj).encode()


class ArchiveCRDTests(unittest.TestCase):
    def setUp(self):
        self.static = resource("static.example.org")
        self.template = resource("template.example.org")
        self.source = "demo/charts/dep/crds/inventory.yaml"
        dep = chart({"dep/Chart.yaml": b"name: dep\nversion: '1'\n",
                     "dep/crds/inventory.yaml": yaml.safe_dump(self.static).encode()})
        package = chart({"demo/Chart.yaml": b"name: demo\nversion: '1'\ndependencies: [{name: dep, version: '1'}]\n",
                         "demo/charts/dep-1.tgz": dep,
                         "demo/files/embedded/Chart.yaml": b"name: embedded\nversion: '1'\n",
                         "demo/files/embedded/crds/unused.yaml": b"ordinary chart data"})
        self.base = "releases/default/jaeger/"
        self.row = {"id":"default/jaeger", "helm":{"operation":"upgrade","skipCRDs":False},
                    "chart":{"name":"demo","version":"1","sha256":digest(package)}}
        self.files = {self.base+"chart.tgz":package}
        self.marker = document(self.source, self.static)
        self.ordinary = document("demo/templates/another-crd.yaml", self.template)

    def render(self, data):
        self.files[self.base+"rendered.yaml"] = data
        return transaction_render(self.row, self.files)

    def test_same_kind_template_crd_stays_in_postrender_stream(self):
        self.assertEqual(self.ordinary, self.render(self.marker + self.ordinary))
        self.assertEqual(self.ordinary, self.render(self.ordinary + self.marker))

    def test_source_identity_content_completeness_and_duplicates_are_checked(self):
        invalid = [self.ordinary, self.marker + self.marker, self.marker.replace(b"example.org", b"other.example"),
                   document("demo/templates/spoof.yaml", self.static), self.marker + document("demo/templates/duplicate.yaml", self.static)]
        for data in invalid:
            with self.subTest(data=data[:50]), self.assertRaises(BundleError):
                self.render(data)

    def test_first_install_refuses_static_cluster_resources(self):
        self.row["helm"]["operation"] = "install"
        for skip in (False, True):
            self.row["helm"]["skipCRDs"] = skip
            with self.assertRaisesRegex(BundleError, "ownership scope"):
                self.render(self.marker+self.ordinary)

    def test_exact_hook_maps_and_applied_file_for_monitoring(self):
        name = "prometheus-crds-upgrade"
        meta = {"name":name,"namespace":"default","annotations":{"helm.sh/hook":"pre-install,pre-upgrade,pre-rollback"}}
        packed = bz2.compress(yaml.safe_dump(self.static).encode())
        cfg = {"apiVersion":"v1","kind":"ConfigMap","metadata":meta,
               "binaryData":{"crds.bz2":base64.b64encode(packed).decode()}}
        tmp = {"name":"tmp","mountPath":"/tmp"}
        init = {"command":["sh"],"args":["-c","bzcat /crds/crds.bz2 > /tmp/crds.yaml"],
                "volumeMounts":[tmp,{"name":"crds","mountPath":"/crds/"}]}
        apply = {"command":["kubectl"],"args":["apply","--server-side","--filename","/tmp/crds.yaml"],"volumeMounts":[tmp]}
        job = {"apiVersion":"batch/v1","kind":"Job","metadata":meta,"spec":{"template":{"spec":{
               "volumes":[{"name":"crds","configMap":{"name":name}},{"name":"tmp","emptyDir":{}}],
               "initContainers":[init],"containers":[apply]}}}}
        self.row["id"] = "default/prometheus"
        self.files["releases/default/prometheus/chart.tgz"] = self.files.pop(self.base+"chart.tgz")
        self.base = "releases/default/prometheus/"
        def ordinary():
            return document("demo/templates/crds-cm.yaml", cfg) + document("demo/templates/crds-job.yaml", job)
        self.assertEqual(ordinary(), self.render(self.marker + ordinary()))
        for wrong in (bz2.compress(yaml.safe_dump(self.template).encode()), packed + bz2.compress(b"duplicate")):
            cfg["binaryData"]["crds.bz2"] = base64.b64encode(wrong).decode()
            with self.assertRaisesRegex(BundleError, "payload"):
                self.render(self.marker + ordinary())
        cfg["binaryData"]["crds.bz2"] = base64.b64encode(packed).decode()
        init["volumeMounts"][1]["subPath"] = "other"
        with self.assertRaisesRegex(BundleError, "mount"):
            self.render(self.marker + ordinary())
        del init["volumeMounts"][1]["subPath"]
        apply["volumeMounts"].append({"name":"crds", "mountPath":"/tmp/crds.yaml", "subPath":"crds.bz2"})
        with self.assertRaisesRegex(BundleError, "shadowed"):
            self.render(self.marker + ordinary())
        apply["volumeMounts"].pop()
        cfg["binaryData"]["crds.bz2"] = "invalid!"
        with self.assertRaisesRegex(BundleError, "encoding"):
            self.render(self.marker + ordinary())
