# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Credential-free tests of the live drift report.

testdata/live-drift holds the sanitized fixture: rendered.yaml (objects by Helm
release, plus the cluster-scoped kinds), live.yaml (objects by identity, null for
a 404) and expected.yaml (each identity's class, reasons and unexplained fields).
Every UID, address and secret in it is fake.
"""

import base64
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import yaml

import live_drift as d


FIXTURE = Path(__file__).parent / "testdata/live-drift"


def load(name):
    return yaml.safe_load((FIXTURE / name).read_text())


def fixture():
    rendered = load("rendered.yaml")
    cluster = set(rendered["clusterScoped"])
    pairs = [(release, obj) for release, objs in rendered["releases"].items() for obj in objs]
    return pairs, (lambda api, kind: f"{api}:{kind}" not in cluster), load("live.yaml")["objects"]


def one(release, render, live, cluster=False, extra=None, grafana_lookup=True):
    """Classify one rendered object against one live object."""
    items = d.plan([(release, render)] + list(extra or []), lambda api, kind: not cluster)
    key = d.identity(release, render, lambda api, kind: not cluster)
    return d.classify(key, items, {key: live}, d._schemas(items), grafana_lookup)


def owned(meta, release, uid="00000000-0000-4000-8000-00000000aaaa"):
    ns, name = release.split("/")
    meta = copy.deepcopy(meta)
    meta.setdefault("labels", {})["app.kubernetes.io/managed-by"] = "Helm"
    meta.setdefault("annotations", {}).update({"meta.helm.sh/release-name": name, "meta.helm.sh/release-namespace": ns})
    meta.setdefault("namespace", ns)
    return dict(meta, uid=uid, resourceVersion="1")


def service(spec, live_spec, release="default/jaeger"):
    render = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "s"}, "spec": spec}
    live = {"apiVersion": "v1", "kind": "Service", "metadata": owned({"name": "s"}, release), "spec": live_spec}
    return one(release, render, live)


class FixtureTests(unittest.TestCase):
    def setUp(self):
        rendered, namespaced, self.live = fixture()
        self.items = d.plan(rendered, namespaced)
        self.report = d.compare(self.items, self.live, grafana_lookup=True)

    def test_each_identity_gets_its_expected_class_reasons_and_fields(self):
        expected = load("expected.yaml")["objects"]
        actual = {e["identity"]: {k: e[k] for k in ("class", "reasons", "unexplainedFields") if k in e}
                  for e in self.report["objects"]}
        self.assertEqual(actual, expected)
        self.assertEqual(self.report["summary"], {"identical": 1, "explained": 14, "unexplained": 7, "missing": 2,
                                                  "total": 24})

    def test_every_reason_code_is_exercised_and_every_used_code_is_in_the_table(self):
        used = {r for e in self.report["objects"] for r in e["reasons"]}
        self.assertEqual(used, set(d.REASONS))

    def test_entries_carry_hashes_and_never_a_raw_uid(self):
        for entry in self.report["objects"]:
            with self.subTest(identity=entry["identity"]):
                obj = self.live[entry["identity"]]
                self.assertRegex(entry["renderSHA256"], "^[0-9a-f]{64}$")
                if obj is None:
                    self.assertIsNone(entry["uidSHA256"])
                    self.assertIsNone(entry["liveSHA256"])
                    continue
                uid = obj["metadata"]["uid"]
                self.assertEqual(entry["uidSHA256"], hashlib.sha256(uid.encode()).hexdigest())
                # The live hash is the one live_observation.py --object prints.
                self.assertEqual(entry["liveSHA256"], d.object_state(entry["identity"], obj)["stateSHA256"])
                if obj["kind"] == "Service":
                    spec = obj["spec"]
                    self.assertEqual(entry["serviceAllocationSHA256"], hashlib.sha256(d.canonical(
                        {"uid": uid, "clusterIP": spec.get("clusterIP"), "clusterIPs": spec.get("clusterIPs")})).hexdigest())
                else:
                    self.assertNotIn("serviceAllocationSHA256", entry)

    def test_identical_objects_hash_the_same_on_both_sides_of_normalization(self):
        identical = [e for e in self.report["objects"] if e["class"] == "identical"]
        self.assertTrue(identical)
        for entry in identical:
            live = d.normalize_live(self.live[entry["identity"]])
            self.assertEqual(entry["renderSHA256"], hashlib.sha256(d.canonical(live)).hexdigest())

    def test_the_report_holds_no_uid_address_or_secret_value(self):
        text = json.dumps(self.report)
        secrets = set()
        for obj in self.live.values():
            if obj is None:
                continue
            secrets.add(obj["metadata"]["uid"])
            spec = obj.get("spec") or {}
            secrets.update(ip for ip in spec.get("clusterIPs", []) if ip != "None")
            for value in (obj.get("data") or {}).values():
                secrets.add(value)
                if obj["kind"] == "Secret":
                    secrets.add(base64.b64decode(value).decode())
            for hook in obj.get("webhooks", []):
                secrets.add(hook["clientConfig"]["caBundle"])
        for release in load("rendered.yaml")["releases"].values():
            for obj in release:
                for value in (obj.get("data") or {}).values():
                    secrets.add(value)
        secrets.discard("")
        self.assertIn("fixture-secret-live-config", secrets)  # guard against a vacuous pass
        self.assertIn("10.96.0.17", secrets)
        for value in sorted(secrets):
            with self.subTest(value=value[:12]):
                self.assertNotIn(value, text)
        # Secret key names stay out of the report too.
        self.assertNotIn("alertmanager.yaml", text)

    def test_scope_requires_all_ten_releases_and_exactly_the_granted_crds(self):
        with self.assertRaisesRegex(ValueError, "ten releases|six application"):
            d.check_scope(self.items)
        crds = [("default/prometheus", {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
                                        "metadata": {"name": name}}) for name in d.MONITORING_CRDS]
        stubs = [(release, {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": release.replace("/", "-")}})
                 for release in sorted(d.RELEASES)]
        cluster = lambda api, kind: kind != "CustomResourceDefinition"  # noqa: E731
        d.check_scope(d.plan(stubs + crds, cluster))
        other = crds[1:] + [("default/prometheus", dict(crds[0][1], metadata={"name": "certificates.cert-manager.io"}))]
        with self.assertRaisesRegex(ValueError, "CRDs"):
            d.check_scope(d.plan(stubs + other, cluster))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            d.plan(stubs + stubs[:1], cluster)


class ReasonTests(unittest.TestCase):
    def test_a_rendered_headless_cluster_ip_must_match_exactly(self):
        spec = {"clusterIP": "None", "ports": [{"port": 80, "targetPort": 80, "protocol": "TCP"}]}
        live = dict(spec, type="ClusterIP", sessionAffinity="None", internalTrafficPolicy="Cluster",
                    clusterIPs=["None"], ipFamilies=["IPv4"], ipFamilyPolicy="SingleStack")
        entry = service(spec, live)
        self.assertEqual((entry["class"], entry["reasons"]), ("explained", ["helm-ownership", "server-default",
                                                                            "service-allocation"]))
        entry = service(spec, dict(live, clusterIP="10.96.0.40", clusterIPs=["10.96.0.40"]))
        self.assertEqual((entry["class"], entry["unexplainedFields"]), ("unexplained", ["spec.clusterIP"]))

    def test_allocation_is_explained_only_where_the_render_leaves_the_field_unset(self):
        spec = {"ports": [{"port": 80, "targetPort": 80, "protocol": "TCP"}], "ipFamilyPolicy": "SingleStack"}
        live = dict(spec, type="ClusterIP", sessionAffinity="None", internalTrafficPolicy="Cluster",
                    clusterIP="10.96.0.41", clusterIPs=["10.96.0.41"], ipFamilies=["IPv4"])
        self.assertEqual(service(spec, live)["class"], "explained")
        entry = service(spec, dict(live, ipFamilyPolicy="PreferDualStack"))
        self.assertEqual(entry["unexplainedFields"], ["spec.ipFamilyPolicy"])
        # An allocation field on a kind other than Service is not explained.
        render = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c"}}
        live = dict(render, metadata=owned({"name": "c"}, "default/jaeger"), spec={"clusterIP": "10.96.0.42"})
        self.assertEqual(one("default/jaeger", render, live)["unexplainedFields"], ["spec.clusterIP"])

    def test_a_server_default_must_have_the_default_value(self):
        spec = {"ports": [{"port": 80, "targetPort": 0}], "clusterIP": "None"}
        live = {"ports": [{"port": 80, "targetPort": 80, "protocol": "TCP"}], "clusterIP": "None",
                "type": "ClusterIP", "sessionAffinity": "None", "internalTrafficPolicy": "Cluster"}
        self.assertEqual(service(spec, live)["reasons"], ["helm-ownership", "server-default"])
        for field, value in (("sessionAffinity", "ClientIP"), ("internalTrafficPolicy", "Local"), ("type", "NodePort")):
            with self.subTest(field=field):
                self.assertEqual(service(spec, dict(live, **{field: value}))["unexplainedFields"], ["spec." + field])
        changed = copy.deepcopy(live)
        changed["ports"][0]["targetPort"] = 81
        self.assertEqual(service(spec, changed)["unexplainedFields"], ["spec.ports[0].targetPort"])

    def test_pull_policy_and_binding_subject_defaults_follow_the_live_object(self):
        self.assertEqual(d._pull_policy({"image": "nginx"}), "Always")
        self.assertEqual(d._pull_policy({"image": "nginx:latest"}), "Always")
        self.assertEqual(d._pull_policy({"image": "registry:5000/nginx"}), "Always")
        self.assertEqual(d._pull_policy({"image": "registry:5000/nginx:1.2"}), "IfNotPresent")
        self.assertEqual(d._pull_policy({"image": "nginx@sha256:" + "a" * 64}), "IfNotPresent")
        self.assertEqual(d._subject_group({"kind": "User"}), "rbac.authorization.k8s.io")
        self.assertEqual(d._subject_group({"kind": "ServiceAccount"}), "")

    def test_quantities_are_explained_only_when_equal_in_value(self):
        self.assertEqual(d.quantity("1000m"), d.quantity("1"))
        self.assertEqual(d.quantity("1024Mi"), d.quantity("1Gi"))
        self.assertEqual(d.quantity("1e3"), d.quantity("1k"))
        self.assertNotEqual(d.quantity("512M"), d.quantity("512Mi"))
        self.assertIsNone(d.quantity("lots"))
        self.assertIsNone(d.quantity(True))

        def deployment(cpu):
            return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "q"},
                    "spec": {"replicas": 1, "selector": {"matchLabels": {"a": "b"}}, "revisionHistoryLimit": 10,
                             "progressDeadlineSeconds": 600, "strategy": {"type": "Recreate"},
                             "template": {"metadata": {"labels": {"a": "b"}}, "spec": {
                                 "dnsPolicy": "ClusterFirst", "restartPolicy": "Always", "schedulerName": "default-scheduler",
                                 "securityContext": {}, "terminationGracePeriodSeconds": 30,
                                 "containers": [{"name": "c", "image": "x:1", "imagePullPolicy": "IfNotPresent",
                                                 "terminationMessagePath": "/dev/termination-log",
                                                 "terminationMessagePolicy": "File",
                                                 "resources": {"limits": {"cpu": cpu}}}]}}}}
        render = deployment("1000m")
        live = deployment("1")
        live["metadata"] = owned({"name": "q"}, "default/jaeger")
        self.assertEqual(one("default/jaeger", render, live)["reasons"], ["helm-ownership", "server-quantity"])
        live["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"] = "2"
        self.assertEqual(one("default/jaeger", render, live)["unexplainedFields"],
                         ["spec.template.spec.containers[0].resources.limits.cpu"])

    def test_controller_fields_are_explained_only_on_the_named_objects(self):
        release = "default/prometheus"
        render = {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "MutatingWebhookConfiguration",
                  "metadata": {"name": "prometheus-kube-prometheus-admission"},
                  "webhooks": [{"name": "w", "clientConfig": {"url": "https://example.test"}, "failurePolicy": "Ignore",
                                "matchPolicy": "Equivalent", "namespaceSelector": {}, "objectSelector": {},
                                "timeoutSeconds": 10, "reinvocationPolicy": "Never", "sideEffects": "None",
                                "admissionReviewVersions": ["v1"]}]}
        live = copy.deepcopy(render)
        live["metadata"] = owned(render["metadata"], release)
        del live["metadata"]["namespace"]
        live["webhooks"][0]["clientConfig"]["caBundle"] = "ZmFrZQ=="
        self.assertEqual(one(release, render, live, cluster=True)["reasons"], ["helm-ownership", "webhook-ca-bundle"])
        render["metadata"]["name"] = live["metadata"]["name"] = "another-webhook"
        self.assertEqual(one(release, render, live, cluster=True)["unexplainedFields"],
                         ["webhooks[0].clientConfig.caBundle"])
        # The kubectl apply annotation is explained on a companion only.
        render = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c", "namespace": "default"}}
        live = dict(render, metadata=dict(owned(render["metadata"], "default/jaeger"), annotations={
            "meta.helm.sh/release-name": "jaeger", "meta.helm.sh/release-namespace": "default",
            "kubectl.kubernetes.io/last-applied-configuration": "{}"}))
        self.assertEqual(one("default/jaeger", render, live)["unexplainedFields"],
                         ["metadata.annotations.kubectl.kubernetes.io/last-applied-configuration"])

    def test_the_operator_validation_annotation_is_explained_only_on_a_prometheus_rule_set_to_true(self):
        release = "default/prometheus"

        def rule(kind="PrometheusRule", value="true"):
            render = {"apiVersion": "monitoring.coreos.com/v1", "kind": kind,
                      "metadata": {"name": "r", "namespace": "default"}, "spec": {"groups": []}}
            live = copy.deepcopy(render)
            live["metadata"] = owned(render["metadata"], release)
            live["metadata"]["annotations"]["prometheus-operator-validated"] = value
            return one(release, render, live)

        self.assertEqual(rule()["reasons"], ["helm-ownership", "prometheus-operator-validated"])
        self.assertEqual(rule()["class"], "explained")
        # Another value, or the same annotation on another kind, is drift, and its key is never printed.
        for entry in (rule(value="false"), rule(kind="ServiceMonitor")):
            self.assertEqual(entry["class"], "unexplained")
            self.assertEqual(entry["unexplainedFields"], ["metadata.annotations.<live-only>"])

    def test_ownership_is_explained_only_for_the_expected_release(self):
        render = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c", "namespace": "default"}}
        live = dict(render, metadata=owned(render["metadata"], "default/jaeger"))
        self.assertEqual(one("default/jaeger", render, live)["reasons"], ["helm-ownership"])
        # The same object counted under another release is foreign ownership.
        self.assertEqual(one("default/prometheus", render, live)["class"], "unexplained")
        # Owned by the companion release while the render places it in an application release.
        live = dict(render, metadata=owned(render["metadata"], "default/demo-resources"))
        self.assertEqual(one("default/jaeger", render, live)["class"], "unexplained")
        # A companion may be unadopted, but only with no Helm metadata at all.
        render = dict(render, metadata=dict(render["metadata"], annotations={"helm.sh/resource-policy": "keep"}))
        bare = dict(render, metadata={"name": "c", "namespace": "default", "uid": "u-1"})
        self.assertEqual(one("default/demo-resources", render, bare)["reasons"], ["companion-unadopted"])
        for partial in ({"labels": {"app.kubernetes.io/managed-by": "Helm"}},
                        {"annotations": {"meta.helm.sh/release-name": "demo-resources"}},
                        {"annotations": {"meta.helm.sh/release-name": "demo-resources",
                                         "meta.helm.sh/release-namespace": "default"}}):
            with self.subTest(partial=partial):
                live = dict(bare, metadata=dict(bare["metadata"], **partial))
                self.assertEqual(one("default/demo-resources", render, live)["class"], "unexplained")
        # Adopted but without the keep policy the ownership patch adds.
        adopted = dict(render, metadata=owned({"name": "c", "namespace": "default"}, "default/demo-resources"))
        self.assertEqual(one("default/demo-resources", render, adopted)["unexplainedFields"],
                         ["metadata.annotations.helm.sh/resource-policy"])
        adopted["metadata"]["annotations"]["helm.sh/resource-policy"] = "keep"
        self.assertEqual(one("default/demo-resources", render, adopted)["reasons"], ["helm-ownership"])

    def test_configmap_and_secret_content_is_never_explained(self):
        for kind, field in (("ConfigMap", "data"), ("Secret", "data")):
            render = {"apiVersion": "v1", "kind": kind, "metadata": {"name": "c", "namespace": "default"},
                      "data": {"k": "YQ=="}}
            live = dict(render, metadata=owned(render["metadata"], "default/jaeger"), data={"k": "Yg=="})
            if kind == "Secret":
                live["type"] = "Opaque"
            with self.subTest(kind=kind):
                self.assertEqual(one("default/jaeger", render, live)["unexplainedFields"], [field])
        # stringData is compared as the base64 data the API server stores.
        render = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "s", "namespace": "default"},
                  "stringData": {"k": "a"}}
        live = dict(render, metadata=owned(render["metadata"], "default/jaeger"), data={"k": "YQ=="}, type="Opaque")
        del live["stringData"]
        self.assertEqual(one("default/jaeger", render, live)["class"], "explained")

    def test_grafana_password_and_checksum_are_explained_only_when_the_checksum_proves_it(self):
        b64 = lambda s: base64.b64encode(s.encode()).decode()  # noqa: E731
        data = {"admin-user": b64("admin"), "admin-password": b64("synthetic"), "ldap-toml": b64("")}
        live_data = dict(data, **{"admin-password": b64("live-password")})
        secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                  "metadata": {"name": "prometheus-grafana", "namespace": "default"}, "data": data}
        live_secret = dict(secret, data=live_data, metadata=owned(secret["metadata"], "default/prometheus", "u-s"))

        def grafana(rendered_sum, live_sum, live_secret=live_secret):
            dep = {"apiVersion": "apps/v1", "kind": "Deployment",
                   "metadata": {"name": "prometheus-grafana", "namespace": "default"},
                   "spec": {"replicas": 1, "template": {"metadata": {"annotations": {"checksum/secret": rendered_sum}}}}}
            live = copy.deepcopy(dep)
            live["metadata"] = owned(dep["metadata"], "default/prometheus")
            live["spec"]["template"]["metadata"]["annotations"]["checksum/secret"] = live_sum
            items = d.plan([("default/prometheus", dep), ("default/prometheus", secret)], lambda api, kind: True)
            key = "apps/v1:Deployment:default:prometheus-grafana"
            return d.classify(key, items, {key: live, d.GRAFANA_SECRET: live_secret}, {}, True)

        rendered_sum = d.grafana_checksum(data, data["admin-password"], True)
        for quoted in (False, True):
            entry = grafana(rendered_sum, d.grafana_checksum(data, live_data["admin-password"], quoted))
            self.assertEqual(entry["reasons"], ["grafana-admin-password", "helm-ownership"])
        for bad in [(rendered_sum, d.grafana_checksum(data, b64("other"), False), live_secret),
                    ("0" * 64, d.grafana_checksum(data, live_data["admin-password"], False), live_secret),
                    (rendered_sum, d.grafana_checksum(data, live_data["admin-password"], False), None)]:
            with self.subTest(bad=bad[:2]):
                self.assertEqual(grafana(*bad)["unexplainedFields"],
                                 ["spec.template.metadata.annotations.checksum/secret"])
        # The password is explained on that Secret only, and the other keys are compared.
        self.assertEqual(one("default/prometheus", secret, live_secret)["reasons"],
                         ["grafana-admin-password", "helm-ownership"])
        changed = dict(live_secret, data=dict(live_data, **{"admin-user": b64("root")}))
        self.assertEqual(one("default/prometheus", secret, changed)["unexplainedFields"], ["data"])
        other = dict(secret, metadata={"name": "other", "namespace": "default"})
        self.assertEqual(one("default/prometheus", other, dict(live_secret, metadata=owned(
            other["metadata"], "default/prometheus")))["class"], "unexplained")

    def test_crd_schema_defaults_apply_to_custom_resources_only_with_the_default_value(self):
        crd = {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
               "metadata": {"name": "widgets.example.test"},
               "spec": {"group": "example.test", "names": {"kind": "Widget", "plural": "widgets"}, "scope": "Namespaced",
                        "versions": [{"name": "v1", "served": True, "storage": True, "schema": {"openAPIV3Schema": {
                            "type": "object", "properties": {"spec": {"type": "object", "properties": {
                                "mode": {"type": "string", "default": "fast"},
                                "selector": {"type": "object"}}}}}}}]}}
        render = {"apiVersion": "example.test/v1", "kind": "Widget", "metadata": {"name": "w", "namespace": "default"},
                  "spec": {"selector": {}}}
        live = dict(copy.deepcopy(render), metadata=owned(render["metadata"], "default/prometheus"))
        live["spec"]["mode"] = "fast"
        extra = [("default/prometheus", crd)]
        self.assertEqual(one("default/prometheus", render, live, extra=extra)["reasons"],
                         ["crd-schema-default", "helm-ownership"])
        live["spec"]["mode"] = "slow"
        self.assertEqual(one("default/prometheus", render, live, extra=extra)["unexplainedFields"], ["spec.mode"])
        # An empty selector is meaningful in a custom resource and is not dropped.
        del live["spec"]["selector"]
        live["spec"]["mode"] = "fast"
        self.assertEqual(one("default/prometheus", render, live, extra=extra)["unexplainedFields"], ["spec.selector"])

    def test_absent_hooks_are_explained_only_with_the_hook_succeeded_policy(self):
        def job(annotations):
            return {"apiVersion": "batch/v1", "kind": "Job",
                    "metadata": {"name": "j", "namespace": "default", "annotations": annotations}}
        for annotations, cls in (({"helm.sh/hook": "post-upgrade", "helm.sh/hook-delete-policy": "hook-succeeded"},
                                  "explained"),
                                 ({"helm.sh/hook": "post-upgrade", "helm.sh/hook-delete-policy": "before-hook-creation"},
                                  "missing"),
                                 ({"helm.sh/hook-delete-policy": "hook-succeeded"}, "missing")):
            with self.subTest(annotations=annotations):
                self.assertEqual(one("default/prometheus", job(annotations), None)["class"], cls)

    def test_a_terminating_or_foreign_controlled_object_is_drift(self):
        render = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c", "namespace": "default"}}
        for extra in ({"deletionTimestamp": "2026-11-24T00:00:00Z"}, {"finalizers": ["example.test/hold"]},
                      {"ownerReferences": [{"kind": "Other", "name": "x", "uid": "u"}]}):
            with self.subTest(extra=extra):
                live = dict(render, metadata=dict(owned(render["metadata"], "default/jaeger"), **extra))
                self.assertEqual(one("default/jaeger", render, live)["class"], "unexplained")


class ReadTests(unittest.TestCase):
    def test_live_images_feed_the_render_with_reference_and_pull_policy(self):
        digest = "sha256:" + "c" * 64
        refs = {("oci", "jaeger"): "docker.io/jaegertracing/jaeger-snapshot:0012345",
                ("oci", "hotrod"): "docker.io/jaegertracing/example-hotrod-snapshot:fedc123",
                ("otel", "jaeger"): "docker.io/jaegertracing/jaeger-snapshot:12ef000@" + digest,
                ("otel", "hotrod"): "docker.io/jaegertracing/example-hotrod-snapshot:feed000"}
        observed = {"images": {stack: {c: {"reference": refs[stack, c]} for c in d.REPOS} for stack in d.STACKS}}

        def get(url):
            ns, workload = re.fullmatch(r"/apis/apps/v1/namespaces/([^/]+)/deployments/([^/]+)", url).groups()
            stack = {v: k for k, v in d.STACKS.items()}[ns]
            component = "hotrod" if workload == "jaeger-hotrod" else "jaeger"
            policy = "IfNotPresent" if (stack, component) == ("otel", "jaeger") else "Always"
            return {"spec": {"template": {"spec": {"containers": [
                {"name": "sidecar", "image": "other"},
                {"name": workload, "image": refs[stack, component], "imagePullPolicy": policy}]}}}}

        with patch.object(d, "snapshot", return_value=observed) as snap:
            images = d.live_images(get, resolve_image="resolver")
        snap.assert_called_once_with(get, [], ["oci", "otel"], "resolver")
        self.assertEqual(images["otel"]["jaeger"], {"repository": "jaegertracing/jaeger-snapshot",
                                                    "tag": "12ef000@" + digest, "pullPolicy": "IfNotPresent"})
        self.assertEqual(images["oci"]["hotrod"], {"repository": "jaegertracing/example-hotrod-snapshot",
                                                   "tag": "fedc123", "pullPolicy": "Always"})
        # A reference that changed between the two reads, or another repository, stops the report.
        for broken in ({("oci", "jaeger"): "docker.io/jaegertracing/jaeger-snapshot:0099999"},
                       {("otel", "hotrod"): "docker.io/someone/example-hotrod-snapshot:feed000"}):
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                changed = copy.deepcopy(observed)
                for (stack, component), ref in broken.items():
                    changed["images"][stack][component]["reference"] = ref
                with patch.object(d, "snapshot", return_value=changed):
                    d.live_images(get)

    def test_the_helmfile_render_receives_the_live_images(self):
        images = {"oci": {"jaeger": {"repository": "r", "tag": "t", "pullPolicy": "Always"}}}
        seen, templates = {}, {}
        fixture_password = "grafana.adminPassword=synthetic-fixture-password"

        def child(root, env, *args):
            self.assertEqual(env["KUBECONFIG"], "/dev/null")  # the render never reaches a cluster
            if "list" in args:
                seen[args[2]] = json.loads(Path(args[args.index("--state-values-file") + 1]).read_text())
                return json.dumps([{"namespace": "default", "name": name, "enabled": True, "installed": True}
                                   for name in ("jaeger", "prometheus")] if args[2] == "helmfile.yaml" else []).encode()
            if "template" in args:
                self.assertIn("--include-crds", args)
                name = args[args.index("-l") + 1].split("name=")[1]
                templates[name] = args
                return f"apiVersion: v1\nkind: ConfigMap\nmetadata: {{name: {name}}}\n---\n".encode()
            return b'{"companions": {}}' if "scripts/demo/companion_values.py" in args else b""

        for lookup in (True, False):
            with self.subTest(grafana_lookup=lookup), tempfile.TemporaryDirectory() as tmp, \
                    patch.object(d.pr_render_diff, "child", side_effect=child):
                templates.clear()
                rendered = d.render_objects(d.ROOT, Path(tmp) / "render", images, lookup)
                self.assertEqual(seen["helmfile.yaml"], {"images": images})
                self.assertEqual([release for release, _ in rendered], ["default/jaeger", "default/prometheus"])
                # The fixture password stands in for the chart's lookup only; otherwise the checkout's values apply.
                self.assertEqual(fixture_password in templates["prometheus"], lookup)
                self.assertNotIn(fixture_password, templates["jaeger"])
        # The pull request render summary keeps the fixture password.
        with tempfile.TemporaryDirectory() as tmp, patch.object(d.pr_render_diff, "child", side_effect=child):
            templates.clear()
            d.pr_render_diff.render(d.ROOT, Path(tmp) / "render", False)
        self.assertIn(fixture_password, templates["prometheus"])

    def test_report_reads_named_objects_only_and_renders_with_the_live_images(self):
        rendered, namespaced, live = fixture()
        endpoints = {"/api": {"kind": "APIVersions", "versions": ["v1"]}, "/version": {"gitVersion": "v1.37.0"}}
        groups = {}
        for release, obj in rendered:
            api, kind = obj["apiVersion"], obj["kind"]
            plural = kind.lower() + "s"
            groups.setdefault(api, {})[kind] = {"name": plural, "kind": kind, "namespaced": namespaced(api, kind),
                                                "verbs": ["get"]}
        endpoints["/apis"] = {"kind": "APIGroupList", "groups": [
            {"versions": [{"groupVersion": api}]} for api in sorted(groups) if "/" in api]}
        objects = {}
        for api, kinds in groups.items():
            base = ("/apis/" if "/" in api else "/api/") + api
            endpoints[base] = {"kind": "APIResourceList", "groupVersion": api, "resources": list(kinds.values())}
        routes, _ = d.discover(endpoints.get)
        for key, obj in live.items():
            objects[d.item_path(key, routes)] = obj
        requested = []

        def get(url, accept="application/json"):
            requested.append(url)
            return endpoints[url] if url in endpoints else objects.get(url)

        @contextlib.contextmanager
        def open_reader(kubeconfig, context):
            self.assertEqual((kubeconfig, context), ("/dev/null", "ctx"))
            yield get

        images = {"oci": {}, "otel": {}}
        with patch.object(d, "live_images", return_value=images) as read_images, \
                patch.object(d, "render_objects", return_value=rendered) as render, \
                patch.object(d, "check_scope"), \
                patch.object(d.pr_render_diff, "revision", return_value="a" * 40):
            result = d.report("/dev/null", "ctx", resolve_image="resolver", open_reader=open_reader)
        read_images.assert_called_once()
        self.assertIs(render.call_args.args[2], images)
        self.assertIs(render.call_args.args[3], True)  # this checkout leaves the password to the lookup
        self.assertEqual(result["renderInputs"]["revision"], "a" * 40)
        self.assertEqual(result["renderInputs"]["imagesSHA256"], hashlib.sha256(d.canonical(images)).hexdigest())
        self.assertEqual(result["summary"]["total"], len(live))
        object_reads = [url for url in requested if url not in endpoints]
        self.assertEqual(sorted(object_reads), sorted(objects))
        for url in requested:
            self.assertNotIn("?", url)  # no collection list, selector or watch
        crd_reads = [url for url in requested if "customresourcedefinitions" in url]
        self.assertTrue(crd_reads)
        for url in crd_reads:
            self.assertRegex(url, r"^/apis/apiextensions\.k8s\.io/v1/customresourcedefinitions/[a-z.]+$")
        # A checkout that sets its own Grafana password renders without the fixture password.
        with patch.object(d, "live_images", return_value=images), \
                patch.object(d, "render_objects", return_value=rendered) as render, \
                patch.object(d, "grafana_uses_lookup", return_value=False), patch.object(d, "check_scope"), \
                patch.object(d.pr_render_diff, "revision", return_value="a" * 40):
            d.report("/dev/null", "ctx", resolve_image="resolver", open_reader=open_reader)
        self.assertIs(render.call_args.args[3], False)

    def test_main_exit_status_and_failure_output(self):
        def run(result=None, error=None):
            out, err = io.StringIO(), io.StringIO()
            with patch.object(d, "report", side_effect=error, return_value=result), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    status = d.main(["--kubeconfig", "/dev/null", "--context", "ctx"])
                except SystemExit as exit:
                    status = exit.code
            return status, out.getvalue(), err.getvalue()

        def summary(**counts):
            return {"objects": [], "summary": dict({"identical": 0, "explained": 0, "unexplained": 0, "missing": 0},
                                                   **counts)}
        self.assertEqual(run(summary(explained=3))[0], 0)
        self.assertEqual(run(summary(unexplained=1))[0], 1)
        self.assertEqual(run(summary(missing=1))[0], 1)
        status, out, err = run(error=ValueError("password=fixture-secret-in-exception"))
        self.assertEqual((status, out), (2, ""))
        self.assertNotIn("fixture-secret", err)
        self.assertIn("ValueError", err)
        # Any other exception type is caught the same way.
        status, out, err = run(error=RuntimeError("fixture-secret-in-runtime-error"))
        self.assertEqual((status, out), (2, ""))
        self.assertNotIn("fixture-secret", err)
        self.assertIn("RuntimeError", err)


def summary_result(**counts):
    return {"objects": [], "summary": dict({"identical": 0, "explained": 0, "unexplained": 0, "missing": 0}, **counts)}


def deployment(spec=None, pod=None, container=None):
    """A Deployment with every server default already set, so that tests change one field."""
    c = {"name": "c", "image": "x:1", "imagePullPolicy": "IfNotPresent", "resources": {},
         "terminationMessagePath": "/dev/termination-log", "terminationMessagePolicy": "File", **(container or {})}
    p = {"dnsPolicy": "ClusterFirst", "restartPolicy": "Always", "schedulerName": "default-scheduler",
         "securityContext": {}, "terminationGracePeriodSeconds": 30, "containers": [c], **(pod or {})}
    s = {"replicas": 1, "revisionHistoryLimit": 10, "progressDeadlineSeconds": 600,
         "selector": {"matchLabels": {"a": "b"}}, "strategy": {"type": "RollingUpdate", "rollingUpdate": {
             "maxSurge": "25%", "maxUnavailable": "25%"}},
         "template": {"metadata": {"labels": {"a": "b"}}, "spec": p}, **(spec or {})}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "w", "namespace": "default"}, "spec": s}


def as_live(obj, release="default/jaeger"):
    live = copy.deepcopy(obj)
    live["metadata"] = owned(obj["metadata"], release)
    return live


class ReviewRegressionTests(unittest.TestCase):
    """Differences that an earlier version explained although they are real drift."""

    def test_output_is_written_outside_the_checkout_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, err = io.StringIO(), io.StringIO()
            target = Path(tmp) / "report.json"
            with patch.object(d, "report", return_value=summary_result(explained=1)) as report, \
                    contextlib.redirect_stdout(out):
                status = d.main(["--kubeconfig", "k", "--context", "c", "--output", str(target)])
            report.assert_called_once()
            self.assertEqual((status, out.getvalue()), (0, ""))
            self.assertEqual(json.loads(target.read_text())["summary"]["explained"], 1)
            for inside in (d.ROOT / "live-drift-report.json", d.ROOT / "docs" / "report.json", d.ROOT):
                with self.subTest(inside=inside), patch.object(d, "report") as report, \
                        contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as exit:
                    d.main(["--kubeconfig", "k", "--context", "c", "--output", str(inside)])
                self.assertEqual(exit.exception.code, 2)
                report.assert_not_called()
                self.assertFalse((d.ROOT / "live-drift-report.json").exists())
            self.assertIn("outside the repository checkout", err.getvalue())

    def test_a_rendered_zero_counts_as_unset_only_where_the_api_defaults_it(self):
        drift = [({"spec": {"replicas": 0}}, "spec.replicas"),
                 ({"pod": {"terminationGracePeriodSeconds": 0}}, "spec.template.spec.terminationGracePeriodSeconds"),
                 ({"spec": {"strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxSurge": "25%", "maxUnavailable": 0}}}},
                  "spec.strategy.rollingUpdate.maxUnavailable")]
        for change, path in drift:
            with self.subTest(path=path):
                render = deployment(**change)
                self.assertEqual(one("default/jaeger", render, as_live(deployment()))["unexplainedFields"], [path])
        # A non-pointer probe threshold or enum that is zero is defaulted by the API server.
        probe = {"readinessProbe": {"tcpSocket": {"port": 80}, "timeoutSeconds": 0, "periodSeconds": 10,
                                    "successThreshold": 1, "failureThreshold": 3}}
        live = deployment(container=dict(probe, readinessProbe=dict(probe["readinessProbe"], timeoutSeconds=1)))
        render = deployment(container=dict(probe, terminationMessagePolicy=""))
        self.assertEqual(one("default/jaeger", render, as_live(live))["reasons"], ["helm-ownership", "server-default"])

    def test_a_rendered_false_equals_an_absent_field_only_for_non_pointer_booleans(self):
        for change, path in (({"pod": {"automountServiceAccountToken": False}},
                               "spec.template.spec.automountServiceAccountToken"),
                              ({"container": {"securityContext": {"allowPrivilegeEscalation": False}}},
                               "spec.template.spec.containers[0].securityContext")):
            with self.subTest(path=path):
                self.assertEqual(one("default/jaeger", deployment(**change), as_live(deployment()))["unexplainedFields"],
                                 [path] if "automount" in path else [path + ".allowPrivilegeEscalation"])
        render = deployment(pod={"hostNetwork": False, "hostPID": False},
                            container={"env": [{"name": "E", "value": ""}], "stdin": False})
        live = deployment(container={"env": [{"name": "E"}]})
        self.assertEqual(one("default/jaeger", render, as_live(live))["reasons"], ["helm-ownership"])

    def test_empty_map_entries_are_never_omitted(self):
        base = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c", "namespace": "default"}}
        cases = [(dict(base, data={"k": ""}), "data"), (dict(base, binaryData={"k": ""}), "binaryData"),
                 (dict(base, metadata=dict(base["metadata"], labels={"a": ""})), "metadata.labels.a"),
                 (dict(base, metadata=dict(base["metadata"], annotations={"a": ""})), "metadata.annotations.a")]
        for render, path in cases:
            with self.subTest(path=path):
                self.assertEqual(one("default/jaeger", render, as_live(base))["unexplainedFields"], [path])
        render = deployment(pod={"nodeSelector": {"zone": ""}})
        self.assertEqual(one("default/jaeger", render, as_live(deployment()))["unexplainedFields"],
                         ["spec.template.spec.nodeSelector.zone"])
        # An empty map or list itself is still the same as an absent one.
        render = dict(base, data={}, metadata=dict(base["metadata"], labels={}))
        self.assertEqual(one("default/jaeger", render, as_live(base))["class"], "explained")

    def test_a_headless_none_is_never_an_allocation(self):
        ports = [{"port": 80, "targetPort": 80, "protocol": "TCP"}]
        defaults = {"type": "ClusterIP", "sessionAffinity": "None", "internalTrafficPolicy": "Cluster",
                    "ipFamilies": ["IPv4"], "ipFamilyPolicy": "SingleStack"}
        entry = service({"ports": ports}, dict(defaults, ports=ports, clusterIP="None", clusterIPs=["None"]))
        self.assertEqual(entry["unexplainedFields"], ["spec.clusterIP", "spec.clusterIPs"])
        entry = service({"ports": ports, "clusterIP": "None"},
                        dict(defaults, ports=ports, clusterIP="None", clusterIPs=["None"]))
        self.assertEqual((entry["class"], entry["reasons"]), ("explained", ["helm-ownership", "server-default",
                                                                            "service-allocation"]))
        entry = service({"ports": ports, "clusterIP": "None"},
                        dict(defaults, ports=ports, clusterIP="None", clusterIPs=["10.96.0.50"]))
        self.assertEqual(entry["unexplainedFields"], ["spec.clusterIPs"])

    def test_a_crd_default_needs_its_parent_in_the_render(self):
        crd = {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
               "metadata": {"name": "prometheuses.monitoring.coreos.com"},
               "spec": {"group": "monitoring.coreos.com", "scope": "Namespaced",
                        "names": {"kind": "Prometheus", "plural": "prometheuses"},
                        "versions": [{"name": "v1", "schema": {"openAPIV3Schema": {"type": "object", "properties": {
                            "spec": {"type": "object", "properties": {
                                "thanos": {"type": "object", "properties": {"blockSize": {"type": "string",
                                                                                         "default": "2h"}}},
                                "web": {"type": "object", "default": {},
                                        "properties": {"port": {"type": "string", "default": "web"}}}}}}}}}]}}
        render = {"apiVersion": "monitoring.coreos.com/v1", "kind": "Prometheus",
                  "metadata": {"name": "p", "namespace": "default"}, "spec": {}}
        live = as_live(render, "default/prometheus")
        live["spec"] = {"thanos": {"blockSize": "2h"}}
        extra = [("default/prometheus", crd)]
        # The live object enables the Thanos sidecar that the render does not.
        self.assertEqual(one("default/prometheus", render, live, extra=extra)["unexplainedFields"],
                         ["spec.thanos.blockSize"])
        rendered = dict(render, spec={"thanos": {}})
        self.assertEqual(one("default/prometheus", rendered, live, extra=extra)["reasons"],
                         ["crd-schema-default", "helm-ownership"])
        # A defaulted parent carries its own defaults.
        live["spec"] = {"web": {"port": "web"}}
        self.assertEqual(one("default/prometheus", render, live, extra=extra)["reasons"],
                         ["crd-schema-default", "helm-ownership"])

    def test_a_custom_resource_without_a_rendered_crd_is_not_built_in(self):
        for api, builtin in (("v1", True), ("apps/v1", True), ("networking.k8s.io/v1", True),
                             ("apiextensions.k8s.io/v1", True), ("monitoring.coreos.com/v1", False),
                             ("cert-manager.io/v1", False)):
            self.assertEqual(d.builtin_api(api), builtin, api)
        render = {"apiVersion": "example.test/v1", "kind": "Widget", "metadata": {"name": "w", "namespace": "default"},
                  "spec": {"selector": {}, "enabled": False, "resources": {"limits": {"cpu": "1000m"}}}}
        live = as_live(render, "default/prometheus")
        live["spec"] = {"resources": {"limits": {"cpu": "1"}}}
        self.assertEqual(one("default/prometheus", render, live)["unexplainedFields"],
                         ["spec.enabled", "spec.resources.limits.cpu", "spec.selector"])

    def test_quantities_only_at_quantity_fields(self):
        render = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c", "namespace": "default"},
                  "data": {"storage": "1000m"}}
        live = dict(as_live(render), data={"storage": "1"})
        self.assertEqual(one("default/jaeger", render, live)["unexplainedFields"], ["data"])
        render = deployment(container={"resources": {"limits": {"cpu": 0.5, "memory": "1024Pi"}}})
        live = deployment(container={"resources": {"limits": {"cpu": "500m", "memory": "1Ei"}}})
        self.assertEqual(one("default/jaeger", render, as_live(live))["reasons"], ["helm-ownership", "server-quantity"])
        self.assertEqual(d.quantity("1024Pi"), d.quantity("1Ei"))
        self.assertEqual(d.quantity("2E"), 2 * 10 ** 18)
        self.assertEqual(d.quantity(0.25), d.quantity("250m"))
        self.assertIsNone(d.quantity(float("nan")))

    def test_the_grafana_exception_needs_the_chart_lookup(self):
        self.assertTrue(d.grafana_uses_lookup(d.ROOT))
        state = yaml.safe_load((d.ROOT / "helmfile.yaml").read_text())
        values = yaml.safe_load((d.ROOT / "examples/oci/monitoring-values.yaml").read_text())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "examples/oci").mkdir(parents=True)

            def write(values, state=state):
                (root / "examples/oci/monitoring-values.yaml").write_text(yaml.safe_dump(values))
                (root / "helmfile.yaml").write_text(yaml.safe_dump(state))
                return d.grafana_uses_lookup(root)

            self.assertTrue(write(values))
            grafana = values.get("grafana", {})
            for change in ({"adminPassword": "set-by-checkout"}, {"admin": {"existingSecret": "s"}},
                           {"env": {"GF_SECURITY_ADMIN_PASSWORD": "x"}}, {"enabled": False}):
                with self.subTest(change=change):
                    self.assertFalse(write(dict(values, grafana=dict(grafana, **change))))
            for change in ({"set": [{"name": "grafana.adminPassword", "value": "x"}]},
                           {"values": ["examples/oci/monitoring-values.yaml.gotmpl"]}):
                with self.subTest(change=change):
                    edited = copy.deepcopy(state)
                    release = next(r for r in edited["releases"] if r["name"] == "prometheus")
                    release.update(change)
                    self.assertFalse(write(values, edited))
        b64 = lambda s: base64.b64encode(s.encode()).decode()  # noqa: E731
        secret = {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                  "metadata": {"name": "prometheus-grafana", "namespace": "default"},
                  "data": {"admin-user": b64("admin"), "admin-password": b64("fixture"), "ldap-toml": ""}}
        live = dict(as_live(secret, "default/prometheus"), data=dict(secret["data"], **{"admin-password": b64("live")}))
        self.assertEqual(one("default/prometheus", secret, live)["class"], "explained")
        self.assertEqual(one("default/prometheus", secret, live, grafana_lookup=False)["unexplainedFields"], ["data"])
        # A missing key is drift even where the rendered value is empty.
        del live["data"]["ldap-toml"]
        self.assertEqual(one("default/prometheus", secret, live)["unexplainedFields"], ["data"])

    def test_live_only_map_keys_are_never_printed(self):
        base = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "c", "namespace": "default",
                                                                       "annotations": {"rendered/key": "a"}}}
        live = as_live(base)
        live["metadata"]["annotations"].update({"note/s3cr3t": "x", "rendered/key": "b"})
        live["data"] = {"prod-db-password.txt": "x", "10.96.0.17": "y"}
        entry = one("default/jaeger", base, live)
        self.assertEqual(entry["unexplainedFields"], ["data", "metadata.annotations.<live-only>",
                                                      "metadata.annotations.rendered/key"])
        text = json.dumps(entry)
        for secret in ("s3cr3t", "prod-db-password", "10.96.0.17"):
            self.assertNotIn(secret, text)
        # A custom resource's unknown or map-typed live key is a placeholder as well.
        crd = {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
               "metadata": {"name": "widgets.example.test"},
               "spec": {"group": "example.test", "scope": "Namespaced", "names": {"kind": "Widget", "plural": "widgets"},
                        "versions": [{"name": "v1", "schema": {"openAPIV3Schema": {"type": "object", "properties": {
                            "spec": {"type": "object", "properties": {
                                "replicas": {"type": "integer"},
                                "externalLabels": {"type": "object", "additionalProperties": {"type": "string"}}}}}}}}]}}
        render = {"apiVersion": "example.test/v1", "kind": "Widget", "metadata": {"name": "w", "namespace": "default"},
                  "spec": {"replicas": 1}}
        live = as_live(render, "default/prometheus")
        live["spec"] = {"replicas": 2, "externalLabels": {"tenant-s3cr3t": "x"}, "unknownS3cr3t": 1}
        entry = one("default/prometheus", render, live, extra=[("default/prometheus", crd)])
        self.assertEqual(entry["unexplainedFields"], ["spec.<live-only>", "spec.externalLabels.<live-only>",
                                                      "spec.replicas"])
        self.assertNotIn("s3cr3t", json.dumps(entry))

    def test_live_only_keys_in_a_crd_schema_are_never_printed(self):
        schema = {"type": "object", "properties": {"spec": {"type": "object", "properties": {
            "mode": {"type": "string", "default": "fast"},
            "limits": {"type": "object", "default": {"rendered": 1}}}}}}
        render = {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
                  "metadata": {"name": "widgets.example.test"},
                  "spec": {"group": "example.test", "scope": "Namespaced", "conversion": {"strategy": "None"},
                           "names": {"kind": "Widget", "plural": "widgets"},
                           "versions": [{"name": "v1", "served": True, "storage": True,
                                         "schema": {"openAPIV3Schema": schema}}]}}
        live = as_live(render, "default/prometheus")
        del live["metadata"]["namespace"]
        node = live["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
        node["tokenS3cr3t"] = {"type": "string"}  # a live-only nested property
        node["limits"]["default"]["passwordS3cr3t"] = 2  # a live-only key in an object default
        node["mode"]["default"] = "slow"
        entry = one("default/prometheus", render, live, cluster=True)
        base = "spec.versions[0].schema.openAPIV3Schema.properties.spec.properties"
        self.assertEqual(entry["unexplainedFields"], [base + ".<live-only>", base + ".limits.default.<live-only>",
                                                      base + ".mode.default"])
        self.assertNotIn("S3cr3t", json.dumps(entry))

    def test_live_only_volume_attribute_and_option_keys_are_never_printed(self):
        volumes = [{"name": "v", "csi": {"driver": "d.example.test", "volumeAttributes": {"rendered": "a"}}},
                   {"name": "f", "flexVolume": {"driver": "x/y", "options": {"rendered": "a"}}}]
        render = deployment(pod={"volumes": volumes})
        live = as_live(deployment(pod={"volumes": copy.deepcopy(volumes)}))
        pod = live["spec"]["template"]["spec"]
        pod["volumes"][0]["csi"]["volumeAttributes"]["bucketS3cr3t"] = "x"  # same list length, live-only keys
        pod["volumes"][1]["flexVolume"]["options"]["tokenS3cr3t"] = "y"
        entry = one("default/jaeger", render, live)
        self.assertEqual(entry["unexplainedFields"], ["spec.template.spec.volumes[0].csi.volumeAttributes.<live-only>",
                                                      "spec.template.spec.volumes[1].flexVolume.options.<live-only>"])
        self.assertNotIn("S3cr3t", json.dumps(entry))

    def test_an_empty_crd_schema_value_is_data_not_an_omitted_field(self):
        schema = {"type": "object", "properties": {"spec": {"type": "object", "properties": {
            "limits": {"type": "object", "default": {}},
            "hosts": {"type": "array", "example": []},
            "mode": {"type": "object", "enum": [{}]},
            "default": {"type": "object", "properties": {}}}}}}
        render = {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
                  "metadata": {"name": "widgets.example.test"},
                  "spec": {"group": "example.test", "scope": "Namespaced", "conversion": {"strategy": "None"},
                           "names": {"kind": "Widget", "plural": "widgets"},
                           "versions": [{"name": "v1", "served": True, "storage": True,
                                         "schema": {"openAPIV3Schema": schema}}]}}
        live = as_live(render, "default/prometheus")
        del live["metadata"]["namespace"]
        node = live["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["spec"]["properties"]
        # A property named `default` is a schema: the API server drops its empty `properties`.
        del node["default"]["properties"]
        self.assertEqual(one("default/prometheus", render, live, cluster=True)["class"], "explained")
        del node["limits"]["default"]
        del node["hosts"]["example"]
        node["mode"]["enum"] = [None]
        entry = one("default/prometheus", render, live, cluster=True)
        base = "spec.versions[0].schema.openAPIV3Schema.properties.spec.properties"
        self.assertEqual(entry["unexplainedFields"], [base + ".hosts.example", base + ".limits.default",
                                                      base + ".mode.enum[0]"])
        self.assertTrue(d.schema_payload(("openAPIV3Schema", "properties", "x", "default")))
        self.assertTrue(d.schema_payload(("openAPIV3Schema", "properties", "x", "enum", 0)))
        self.assertFalse(d.schema_payload(("openAPIV3Schema", "properties", "x", "enum")))
        self.assertFalse(d.schema_payload(("openAPIV3Schema", "properties", "default")))
        self.assertFalse(d.schema_payload(("openAPIV3Schema", "properties", "default", "properties")))
        self.assertFalse(d.schema_payload(("spec", "template", "default")))

    def test_an_empty_label_selector_is_not_an_omitted_map(self):
        pdb = {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": {"name": "p", "namespace": "default"},
               "spec": {"maxUnavailable": 1}}
        # An empty LabelSelector selects every Pod; an absent one selects none.
        entry = one("default/jaeger", dict(pdb, spec={"maxUnavailable": 1, "selector": {}}), as_live(pdb))
        self.assertEqual(entry["unexplainedFields"], ["spec.selector"])
        # Its empty matchLabels map is omitted like any other empty map.
        live = as_live(dict(pdb, spec={"maxUnavailable": 1, "selector": {}}))
        render = dict(pdb, spec={"maxUnavailable": 1, "selector": {"matchLabels": {}}})
        self.assertEqual(one("default/jaeger", render, live)["class"], "explained")
        # A Service selector is a plain map: empty and absent are the same.
        svc = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "s", "namespace": "default"},
               "spec": {"type": "ExternalName", "externalName": "example.test"}}
        render = dict(svc, spec=dict(svc["spec"], selector={}))
        self.assertEqual(one("default/jaeger", render, as_live(svc))["class"], "explained")
        # An empty struct that a pointer holds is kept by the API server.
        entry = one("default/jaeger", deployment(container={"securityContext": {}}), as_live(deployment()))
        self.assertEqual(entry["unexplainedFields"], ["spec.template.spec.containers[0].securityContext"])

    def test_a_missing_crd_makes_its_objects_missing_without_reading_them(self):
        rendered, _, live = fixture()
        # This test covers kinds whose CustomResourceDefinition the fixture renders. The fixture renders a
        # PrometheusRule object but not its CRD, so with that kind absent from discovery the planner refuses it as
        # unknown before any read. Leave that object out here.
        rendered = [(release, obj) for release, obj in rendered if obj["kind"] != "PrometheusRule"]
        crds = [obj for _, obj in rendered if obj["kind"] == "CustomResourceDefinition"]
        self.assertTrue(crds)
        groups = {}
        for _, obj in rendered:
            if obj["apiVersion"].startswith("monitoring.coreos.com/"):
                continue  # the cluster lacks the monitoring CRDs
            cluster = obj["kind"] in ("CustomResourceDefinition", "ValidatingWebhookConfiguration")
            groups.setdefault(obj["apiVersion"], {})[obj["kind"]] = {
                "name": obj["kind"].lower() + "s", "kind": obj["kind"], "namespaced": not cluster, "verbs": ["get"]}
        endpoints = {"/api": {"kind": "APIVersions", "versions": ["v1"]}, "/version": {"gitVersion": "v1.37.0"},
                     "/apis": {"kind": "APIGroupList", "groups": [
                         {"versions": [{"groupVersion": api}]} for api in sorted(groups) if "/" in api]}}
        for api, kinds in groups.items():
            endpoints[("/apis/" if "/" in api else "/api/") + api] = {
                "kind": "APIResourceList", "groupVersion": api, "resources": list(kinds.values())}
        requested = []

        def get(url, accept="application/json"):
            requested.append(url)
            return endpoints.get(url)

        items, read = d.read_live(get, rendered)
        monitoring = [key for key in items if key.startswith("monitoring.coreos.com/")]
        self.assertEqual(monitoring, ["monitoring.coreos.com/v1:ServiceMonitor:default:prometheus-grafana"])
        self.assertIsNone(read[monitoring[0]])
        self.assertFalse([url for url in requested if "monitoring.coreos.com/v1" in url])
        result = d.compare(items, read, True)
        entry = next(e for e in result["objects"] if e["identity"] == monitoring[0])
        self.assertEqual(entry["class"], "missing")
        # Ambiguous discovery still stops the report.
        endpoints["/apis/apps/v1"]["resources"].append(dict(endpoints["/apis/apps/v1"]["resources"][0], name="other"))
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            d.read_live(get, rendered)


class TableTests(unittest.TestCase):
    def test_docs_list_exactly_the_reason_codes(self):
        text = (d.ROOT / "docs/helmfile-applications.md").read_text()
        section = text.split("## Live drift report", 1)[1].split("\n## ", 1)[0]
        documented = set(re.findall(r"^\| `([a-z-]+)` \|", section, re.M))
        self.assertEqual(documented, set(d.REASONS))

    def test_rules_name_only_known_reasons_and_kinds(self):
        for rule in d.RULES:
            with self.subTest(rule=rule[:3]):
                self.assertIn(rule.reason, d.REASONS)
                self.assertTrue(rule.kinds is None or all(":" in kind for kind in rule.kinds))
        # Server defaults are fixed values or functions of the live object, never "any value".
        self.assertFalse([r for r in d.RULES if r.reason == "server-default" and r.value is d.ANY])
        self.assertFalse([r for r in d.RULES if r.reason != "kubectl-last-applied" and r.kinds is None])


if __name__ == "__main__":
    unittest.main()
