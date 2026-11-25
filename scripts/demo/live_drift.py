#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Compare every object the Helmfile render would own with the live object.

The render uses the live Jaeger and HotROD image references of both stacks. Each
object is identical, explained (with reason codes from REASONS), unexplained or
missing. The report holds identities, classes, reason codes, field paths and
SHA-256 hashes. It never holds a field value, Secret data, a raw UID or an API
error body. Exit status: 0 when nothing is unexplained or missing, 1 when
something is, 2 when the report could not be produced.
"""

import argparse
import base64
from collections import namedtuple
import copy
import decimal
import hashlib
import http.client
import json
from pathlib import Path
import re
import subprocess
import tempfile

import yaml

import pr_render_diff
from equivalence import TARGET
from live_observation import RELEASES, canonical, discover, item_path, object_state, reader, require, snapshot
from promotion_images import REPOS, STACKS, registry_image


ROOT = Path(__file__).resolve().parents[2]
COMPANION = "demo-resources"
# The CRDs the monitoring chart renders with --include-crds. ops/rbac/deployer.yaml
# grants a named get on exactly these; the report fails if the render differs.
MONITORING_CRDS = tuple(plural + ".monitoring.coreos.com" for plural in (
    "alertmanagerconfigs", "alertmanagers", "podmonitors", "probes", "prometheusagents",
    "prometheuses", "prometheusrules", "scrapeconfigs", "servicemonitors", "thanosrulers"))
CLASSES = ("identical", "explained", "unexplained", "missing")

# The only reasons a difference may be explained by. docs/helmfile-applications.md lists them.
REASONS = {
    "server-default": "Kubernetes API default for a field the render leaves unset, from a fixed list per kind",
    "server-quantity": "The API server's canonical form of a built-in resource quantity, equal in value to the render",
    "crd-schema-default": "Default declared in the rendered CRD schema for a custom resource field the render leaves unset",
    "helm-ownership": "Complete Helm ownership metadata of the expected release: the application release, "
                      "or demo-resources for an allowlisted companion",
    "companion-unadopted": "Allowlisted companion with no Helm ownership and no helm.sh/resource-policy, "
                           "which the ownership patch adds at adoption",
    "kubectl-last-applied": "kubectl apply annotation on an allowlisted companion, which the scripts create with kubectl apply",
    "service-allocation": "Service clusterIP, clusterIPs, ipFamilies or ipFamilyPolicy that the render leaves unset",
    "webhook-ca-bundle": "caBundle that the admission patch hook writes into the two monitoring webhook configurations",
    "deployment-revision": "deployment.kubernetes.io/revision annotation that the Deployment controller writes",
    "daemonset-generation": "deprecated.daemonset.template.generation annotation that the DaemonSet controller writes",
    "rollout-restart": "kubectl rollout restart annotation in the pod template of a Deployment the scripts restart",
    "grafana-admin-password": "Grafana admin password that the chart keeps from the live Secret by lookup, "
                              "and the pod template checksum computed from it",
    "hook-deleted": "Helm hook with the hook-succeeded delete policy, absent after a successful run",
}

ABSENT = object()
ANY = object()  # a controller or hook writes any value
HELM_LABEL = ("metadata", "labels", "app.kubernetes.io/managed-by")
HELM_RELEASE = ("metadata", "annotations", "meta.helm.sh/release-name")
HELM_NAMESPACE = ("metadata", "annotations", "meta.helm.sh/release-namespace")
KEEP = ("metadata", "annotations", "helm.sh/resource-policy")
TRANSIENT = {"uid", "resourceVersion", "managedFields", "generation", "creationTimestamp", "selfLink"}
ALLOCATION = {("spec", "clusterIP"), ("spec", "clusterIPs"), ("spec", "ipFamilies"), ("spec", "ipFamilyPolicy")}
GRAFANA_SECRET = "v1:Secret:default:prometheus-grafana"
GRAFANA_DEPLOYMENT = "apps/v1:Deployment:default:prometheus-grafana"
GRAFANA_CHECKSUM = ("spec", "template", "metadata", "annotations", "checksum/secret")
RESTARTED = {"apps/v1:Deployment:default:trace-generator", "apps/v1:Deployment:jaeger:trace-generator",
             "apps/v1:Deployment:jaeger:jaeger"}
WEBHOOKS = {"admissionregistration.k8s.io/v1:MutatingWebhookConfiguration:-:prometheus-kube-prometheus-admission",
            "admissionregistration.k8s.io/v1:ValidatingWebhookConfiguration:-:prometheus-kube-prometheus-admission"}

# One rule: a reason, the apiVersion:Kind values it covers, a path pattern ("[]" is
# any list index, "*" any map key), the expected live value (a constant, ANY, or a
# function of the live parent object) and an optional predicate on the context.
Rule = namedtuple("Rule", "reason kinds pattern value where", defaults=(None,))


def _pull_policy(container):
    image = container.get("image", "")
    if "@" in image:
        return "IfNotPresent"
    last = image.rsplit("/", 1)[-1]
    return "Always" if ":" not in last or last.rsplit(":", 1)[1] == "latest" else "IfNotPresent"


def _subject_group(subject):
    return "rbac.authorization.k8s.io" if subject.get("kind") in ("User", "Group") else ""


def _server_defaults():
    pods = {"apps/v1:Deployment": ("spec", "template", "spec"), "apps/v1:StatefulSet": ("spec", "template", "spec"),
            "apps/v1:DaemonSet": ("spec", "template", "spec"), "batch/v1:Job": ("spec", "template", "spec"),
            "batch/v1:CronJob": ("spec", "jobTemplate", "spec", "template", "spec")}
    pod = [(("dnsPolicy",), "ClusterFirst"), (("restartPolicy",), "Always"), (("schedulerName",), "default-scheduler"),
           (("securityContext",), {}), (("terminationGracePeriodSeconds",), 30),
           # The API server mirrors serviceAccountName into the deprecated field.
           (("serviceAccount",), lambda pod: pod.get("serviceAccountName"))]
    container = [(("terminationMessagePath",), "/dev/termination-log"), (("terminationMessagePolicy",), "File"),
                 (("imagePullPolicy",), _pull_policy), (("resources",), {}), (("ports", "[]", "protocol"), "TCP"),
                 (("env", "[]", "valueFrom", "fieldRef", "apiVersion"), "v1")]
    for probe in ("livenessProbe", "readinessProbe", "startupProbe"):
        container += [((probe, "timeoutSeconds"), 1), ((probe, "periodSeconds"), 10), ((probe, "successThreshold"), 1),
                      ((probe, "failureThreshold"), 3), ((probe, "httpGet", "scheme"), "HTTP"),
                      ((probe, "httpGet", "path"), "/"), ((probe, "grpc", "service"), "")]
    for hook in ("postStart", "preStop"):
        container += [(("lifecycle", hook, "httpGet", "scheme"), "HTTP"), (("lifecycle", hook, "httpGet", "path"), "/")]
    volume = [(("configMap", "defaultMode"), 420), (("secret", "defaultMode"), 420), (("projected", "defaultMode"), 420),
              (("downwardAPI", "defaultMode"), 420), (("hostPath", "type"), ""),
              (("downwardAPI", "items", "[]", "fieldRef", "apiVersion"), "v1"),
              (("projected", "sources", "[]", "downwardAPI", "items", "[]", "fieldRef", "apiVersion"), "v1"),
              (("projected", "sources", "[]", "serviceAccountToken", "expirationSeconds"), 3600)]
    rules = []
    for kind, prefix in pods.items():
        rules += [Rule("server-default", {kind}, prefix + path, value) for path, value in pod]
        for field in ("containers", "initContainers"):
            rules += [Rule("server-default", {kind}, prefix + (field, "[]") + path, value) for path, value in container]
        rules += [Rule("server-default", {kind}, prefix + ("volumes", "[]") + path, value) for path, value in volume]
        # An unset pod template metadata reads back as an empty object.
        rules.append(Rule("server-default", {kind}, prefix[:-1] + ("metadata",), {}))
    rules.append(Rule("server-default", {"batch/v1:CronJob"}, ("spec", "jobTemplate", "metadata"), {}))
    workload = {
        "apps/v1:Deployment": [(("replicas",), 1), (("revisionHistoryLimit",), 10), (("progressDeadlineSeconds",), 600),
                               (("strategy", "type"), "RollingUpdate"), (("strategy", "rollingUpdate", "maxSurge"), "25%"),
                               (("strategy", "rollingUpdate", "maxUnavailable"), "25%")],
        "apps/v1:StatefulSet": [(("replicas",), 1), (("revisionHistoryLimit",), 10), (("podManagementPolicy",), "OrderedReady"),
                                (("updateStrategy", "type"), "RollingUpdate"),
                                (("updateStrategy", "rollingUpdate", "partition"), 0),
                                (("updateStrategy", "rollingUpdate", "maxUnavailable"), 1),
                                (("persistentVolumeClaimRetentionPolicy", "whenDeleted"), "Retain"),
                                (("persistentVolumeClaimRetentionPolicy", "whenScaled"), "Retain"),
                                (("volumeClaimTemplates", "[]", "apiVersion"), "v1"),
                                (("volumeClaimTemplates", "[]", "kind"), "PersistentVolumeClaim"),
                                (("volumeClaimTemplates", "[]", "spec", "volumeMode"), "Filesystem"),
                                (("volumeClaimTemplates", "[]", "status", "phase"), "Pending")],
        "apps/v1:DaemonSet": [(("revisionHistoryLimit",), 10), (("updateStrategy", "type"), "RollingUpdate"),
                              (("updateStrategy", "rollingUpdate", "maxUnavailable"), 1),
                              (("updateStrategy", "rollingUpdate", "maxSurge"), 0)],
        "batch/v1:CronJob": [(("concurrencyPolicy",), "Allow"), (("suspend",), False), (("successfulJobsHistoryLimit",), 3),
                             (("failedJobsHistoryLimit",), 1)],
        "batch/v1:Job": [(("backoffLimit",), 6), (("completions",), 1), (("parallelism",), 1),
                         (("completionMode",), "NonIndexed"), (("suspend",), False), (("manualSelector",), False),
                         (("podReplacementPolicy",), "TerminatingOrFailed")],
        "v1:Service": [(("type",), "ClusterIP"), (("sessionAffinity",), "None"), (("internalTrafficPolicy",), "Cluster"),
                       (("ports", "[]", "protocol"), "TCP"), (("ports", "[]", "targetPort"), lambda port: port.get("port"))],
        "apiextensions.k8s.io/v1:CustomResourceDefinition": [(("conversion", "strategy"), "None")],
    }
    for kind, fields in workload.items():
        rules += [Rule("server-default", {kind}, ("spec",) + path, value) for path, value in fields]
    rules.append(Rule("server-default", {"v1:Secret"}, ("type",), "Opaque"))
    bindings = {"rbac.authorization.k8s.io/v1:RoleBinding", "rbac.authorization.k8s.io/v1:ClusterRoleBinding"}
    rules.append(Rule("server-default", bindings, ("subjects", "[]", "apiGroup"), _subject_group))
    webhooks = {"admissionregistration.k8s.io/v1:MutatingWebhookConfiguration",
                "admissionregistration.k8s.io/v1:ValidatingWebhookConfiguration"}
    for path, value in ((("failurePolicy",), "Fail"), (("matchPolicy",), "Equivalent"), (("namespaceSelector",), {}),
                        (("objectSelector",), {}), (("timeoutSeconds",), 10), (("rules", "[]", "scope"), "*"),
                        (("clientConfig", "service", "port"), 443)):
        rules.append(Rule("server-default", webhooks, ("webhooks", "[]") + path, value))
    rules.append(Rule("server-default", {"admissionregistration.k8s.io/v1:MutatingWebhookConfiguration"},
                      ("webhooks", "[]", "reinvocationPolicy"), "Never"))
    return rules


def _companion(ctx):
    return ctx["release"].endswith("/" + COMPANION)


RULES = tuple(_server_defaults()) + (
    Rule("webhook-ca-bundle", {"admissionregistration.k8s.io/v1:MutatingWebhookConfiguration",
                               "admissionregistration.k8s.io/v1:ValidatingWebhookConfiguration"},
         ("webhooks", "[]", "clientConfig", "caBundle"), ANY, lambda ctx: ctx["identity"] in WEBHOOKS),
    Rule("deployment-revision", {"apps/v1:Deployment"}, ("metadata", "annotations", "deployment.kubernetes.io/revision"), ANY),
    Rule("daemonset-generation", {"apps/v1:DaemonSet"},
         ("metadata", "annotations", "deprecated.daemonset.template.generation"), ANY),
    Rule("rollout-restart", {"apps/v1:Deployment"},
         ("spec", "template", "metadata", "annotations", "kubectl.kubernetes.io/restartedAt"), ANY,
         lambda ctx: ctx["identity"] in RESTARTED),
    Rule("kubectl-last-applied", None, ("metadata", "annotations", "kubectl.kubernetes.io/last-applied-configuration"),
         ANY, _companion),
)

QUANTITY = re.compile(r"([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))([eE][+-]?[0-9]+|[KMGTPE]i|[numkMGTPE]?)")
SUFFIX = {"": 1, "n": decimal.Decimal("1e-9"), "u": decimal.Decimal("1e-6"), "m": decimal.Decimal("1e-3"),
          "k": 10 ** 3, "M": 10 ** 6, "G": 10 ** 9, "T": 10 ** 12, "P": 10 ** 15, "E": 10 ** 18,
          "Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40, "Pi": 2 ** 50, "Ei": 2 ** 60}


def quantity(value):
    """The value of a Kubernetes quantity, or None when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    match = QUANTITY.fullmatch(str(value))
    if not match:
        return None
    number, suffix = decimal.Decimal(match[1]), match[2]
    if suffix[:1] in ("e", "E"):
        return number.scaleb(int(suffix[1:]))
    return number * SUFFIX[suffix]


def digest(data):
    return hashlib.sha256(data).hexdigest()


def prune(value):
    """Drop null values, which Kubernetes treats as omitted."""
    if isinstance(value, dict):
        return {k: prune(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [prune(v) for v in value]
    return value


def at(obj, path):
    for key in path:
        if isinstance(key, int):
            if not isinstance(obj, list) or key >= len(obj):
                return ABSENT
        elif not isinstance(obj, dict) or key not in obj:
            return ABSENT
        obj = obj[key]
    return obj


def drop(obj, path):
    """Remove a metadata key and the map that held it if that map is now empty."""
    parent = at(obj, path[:-1])
    if isinstance(parent, dict) and path[-1] in parent:
        del parent[path[-1]]
        if not parent:
            del at(obj, path[:-2])[path[-2]]


def matches(pattern, path):
    return len(pattern) == len(path) and all(
        (p == "[]" and isinstance(k, int)) or (p == "*" and isinstance(k, str)) or p == k
        for p, k in zip(pattern, path))


def same(a, b):
    if a is ABSENT or b is ABSENT:
        return a is b
    return canonical(a) == canonical(b)


def differences(render, live, builtin, path=()):
    """Yield (path, render value, live value) for every leaf that differs."""
    if isinstance(render, dict) and isinstance(live, dict):
        for key in sorted(render.keys() | live.keys()):
            yield from differences(render.get(key, ABSENT), live.get(key, ABSENT), builtin, path + (key,))
    elif render is ABSENT and isinstance(live, dict) and live:
        for key in sorted(live):
            yield from differences(ABSENT, live[key], builtin, path + (key,))
    elif live is ABSENT and isinstance(render, dict) and render:
        for key in sorted(render):
            yield from differences(render[key], ABSENT, builtin, path + (key,))
    elif isinstance(render, list) and isinstance(live, list) and len(render) == len(live):
        for index, (r, v) in enumerate(zip(render, live)):
            yield from differences(r, v, builtin, path + (index,))
    elif live is ABSENT and (builtin or path[:1] == ("metadata",)) and render is not ABSENT and not render:
        return  # a built-in type, and object metadata, omit an empty or zero optional field
    elif render is ABSENT or live is ABSENT or not same(render, live):
        yield path, render, live


def identity(release, obj, namespaced):
    meta = obj["metadata"]
    api, kind = obj["apiVersion"], obj["kind"]
    name = pr_render_diff.identity_field(meta["name"])
    scope = pr_render_diff.identity_field(meta.get("namespace") or release.split("/")[0]) if namespaced(api, kind) else "-"
    return ":".join((api, pr_render_diff.identity_field(kind, pr_render_diff.KIND), scope, name))


def normalize_render(release, obj, namespaced):
    obj = prune(copy.deepcopy(obj))
    meta = obj["metadata"]
    if namespaced(obj["apiVersion"], obj["kind"]):
        meta["namespace"] = meta.get("namespace") or release.split("/")[0]
    else:
        meta.pop("namespace", None)  # the API server ignores a namespace on a cluster-scoped object
    if obj["kind"] == "Secret" and obj["apiVersion"] == "v1" and "stringData" in obj:
        # The API server stores stringData as base64 data.
        data = obj.setdefault("data", {})
        for key, value in obj.pop("stringData").items():
            data[key] = base64.b64encode(str(value).encode()).decode()
    return obj


def normalize_live(obj):
    obj = prune(copy.deepcopy(obj))
    obj.pop("status", None)
    obj["metadata"] = {k: v for k, v in obj["metadata"].items() if k not in TRANSIENT}
    return obj


def plan(rendered, namespaced):
    """Index the render by live identity. Each entry is (release, raw render, normalized render)."""
    items = {}
    for release, obj in rendered:
        key = identity(release, obj, namespaced)
        require(key not in items, "Duplicate rendered identity")
        items[key] = (release, obj, normalize_render(release, obj, namespaced))
    return items


def check_scope(items):
    """The full render holds all ten releases and exactly the CRDs the deployer role may read."""
    releases = {release for release, _, _ in items.values()}
    crds = {raw["metadata"]["name"] for _, raw, _ in items.values() if raw["kind"] == "CustomResourceDefinition"}
    require(releases == RELEASES, "The render does not hold the six application and four companion releases")
    require(crds == set(MONITORING_CRDS), "Rendered CRDs differ from the named CRDs the deployer role reads")


def _schemas(items):
    schemas = {}
    for _, _, obj in items.values():
        if obj["kind"] == "CustomResourceDefinition" and obj["apiVersion"] == "apiextensions.k8s.io/v1":
            spec = obj["spec"]
            for version in spec["versions"]:
                schema = version.get("schema", {}).get("openAPIV3Schema")
                schemas[(spec["group"] + "/" + version["name"], spec["names"]["kind"])] = schema
    return schemas


def _schema_default(schema, path):
    node = schema
    for key in path:
        if not isinstance(node, dict):
            return ABSENT
        if isinstance(key, int):
            node = node.get("items")
        elif key in node.get("properties", {}):
            node = node["properties"][key]
        elif isinstance(node.get("additionalProperties"), dict):
            node = node["additionalProperties"]
        else:
            return ABSENT
    return node.get("default", ABSENT) if isinstance(node, dict) else ABSENT


def grafana_checksum(data, password, quoted):
    """sha256sum of the grafana.secretsData template of the pinned Grafana chart."""
    value = f'"{password}"' if quoted else password
    text = f'\nadmin-user: "{data["admin-user"]}"\nadmin-password: {value}\nldap-toml: "{data["ldap-toml"]}"'
    return digest(text.encode())


def _grafana(ctx, path, render, live):
    if ctx["identity"] == GRAFANA_SECRET and path == ("data", "admin-password"):
        return isinstance(render, str) and isinstance(live, str) and bool(live)
    if ctx["identity"] != GRAFANA_DEPLOYMENT or path != GRAFANA_CHECKSUM:
        return False
    rendered = ctx["items"].get(GRAFANA_SECRET)
    secret = ctx["live"].get(GRAFANA_SECRET)
    if not rendered or not secret:
        return False
    data, live_data = rendered[2].get("data", {}), secret.get("data") or {}
    if set(data) != {"admin-user", "admin-password", "ldap-toml"} or not live_data.get("admin-password"):
        return False
    # The render's own checksum proves this model of the template; the live checksum is
    # the same text with the live password, unquoted after a lookup or quoted at first install.
    return (render == grafana_checksum(data, data["admin-password"], True)
            and live in {grafana_checksum(data, live_data["admin-password"], quoted) for quoted in (False, True)})


def explain(ctx, path, render, live):
    """The reason code that explains one leaf difference, or None."""
    kind = ctx["kind"]
    if _grafana(ctx, path, render, live):
        return "grafana-admin-password"
    # A built-in type treats a zero value (such as targetPort: 0) as unset and defaults it.
    zero = ctx["builtin"] and render is not ABSENT and not render and not isinstance(render, bool)
    if (render is ABSENT or zero) and live is not ABSENT:
        if kind == "v1:Service" and path in ALLOCATION and render is ABSENT:
            return "service-allocation"
        for rule in RULES:
            if ((rule.kinds is None or kind in rule.kinds) and matches(rule.pattern, path)
                    and (rule.where is None or rule.where(ctx))):
                if rule.value is ANY:
                    if render is ABSENT and isinstance(live, str):
                        return rule.reason
                    continue
                expected = rule.value(at(ctx["normalized"], path[:-1])) if callable(rule.value) else rule.value
                if same(expected, live):
                    return rule.reason
        schema = ctx["schemas"].get((ctx["api"], ctx["kind_name"]))
        if schema is not None and path[0] != "metadata":
            default = _schema_default(schema, path)
            if default is not ABSENT and same(default, live):
                return "crd-schema-default"
    if (ctx["builtin"] and render is not ABSENT and live is not ABSENT and len(path) >= 2
            and (path[-2] in ("limits", "requests") or path[-1] in ("sizeLimit", "storage"))):
        a, b = quantity(render), quantity(live)
        if a is not None and a == b and isinstance(live, str):
            return "server-quantity"
    return None


def _ownership(ctx, render, live):
    """Strip Helm ownership the report expects. Return (reasons, unexplained fields)."""
    ns, name = ctx["release"].split("/")
    annotations = (at(live, HELM_RELEASE), at(live, HELM_NAMESPACE))
    label = at(live, HELM_LABEL)
    raw = ctx["raw"]
    optional = (_companion(ctx) or "helm.sh/hook" in (raw["metadata"].get("annotations") or {})
                or raw["kind"] == "CustomResourceDefinition")
    reasons, unexplained = set(), []
    if annotations == (name, ns) and label == "Helm":
        for path in (HELM_LABEL, HELM_RELEASE, HELM_NAMESPACE):
            drop(live, path)
        drop(render, HELM_LABEL)  # Helm sets this label on install whatever the chart renders
        reasons.add("helm-ownership")
    elif optional and annotations == (ABSENT, ABSENT) and same(label, at(render, HELM_LABEL)):
        # No Helm ownership at all. Before adoption a companion also lacks the keep policy.
        if _companion(ctx) and at(live, KEEP) is ABSENT and at(render, KEEP) == "keep":
            drop(render, KEEP)
            reasons.add("companion-unadopted")
    else:
        unexplained.append("metadata.(helm ownership)")  # foreign, partial or missing
    return reasons, unexplained


def field(kind, path):
    if kind == "v1:Secret" and path and path[0] in ("data", "stringData"):
        path = path[:1]  # never name a Secret key
    return "".join(f"[{p}]" if isinstance(p, int) else ("." if i else "") + p for i, p in enumerate(path))


def classify(key, items, live, schemas):
    release, raw, normalized = items[key]
    api, kind_name = raw["apiVersion"], raw["kind"]
    entry = {"identity": key, "release": release, "renderSHA256": digest(canonical(normalized)),
             "uidSHA256": None, "liveSHA256": None, "reasons": []}
    obj = live.get(key)
    if obj is None:
        hook = raw["metadata"].get("annotations") or {}
        policies = {p.strip() for p in hook.get("helm.sh/hook-delete-policy", "").split(",")}
        if "helm.sh/hook" in hook and "hook-succeeded" in policies:
            return dict(entry, **{"class": "explained", "reasons": ["hook-deleted"]})
        return dict(entry, **{"class": "missing"})
    state = object_state(key, obj)  # checks the identity and hashes like live_observation.py --object
    uid = obj["metadata"]["uid"]
    entry.update(uidSHA256=digest(uid.encode()), liveSHA256=state["stateSHA256"])
    if api == "v1" and kind_name == "Service":
        spec = obj.get("spec") or {}
        entry["serviceAllocationSHA256"] = digest(canonical(
            {"uid": uid, "clusterIP": spec.get("clusterIP"), "clusterIPs": spec.get("clusterIPs")}))
    render, current = copy.deepcopy(normalized), normalize_live(obj)
    ctx = {"identity": key, "release": release, "raw": raw, "api": api, "kind_name": kind_name,
           "kind": api + ":" + kind_name, "builtin": (api, kind_name) not in schemas, "schemas": schemas,
           "items": items, "live": live, "normalized": current}
    reasons, unexplained = _ownership(ctx, render, current)
    for path, r, v in differences(render, current, ctx["builtin"]):
        reason = explain(ctx, path, r, v)
        if reason:
            reasons.add(reason)
        else:
            unexplained.append(field(ctx["kind"], path))
    entry["reasons"] = sorted(reasons)
    if unexplained:
        entry.update(unexplainedFields=sorted(set(unexplained)), **{"class": "unexplained"})
    else:
        entry["class"] = "explained" if reasons else "identical"
    return entry


def compare(items, live):
    """Classify each planned identity against `live`, a map from identity to object or None."""
    schemas = _schemas(items)
    entries = [classify(key, items, live, schemas) for key in sorted(items)]
    summary = {name: sum(e["class"] == name for e in entries) for name in CLASSES}
    summary["total"] = len(entries)
    return {"objects": entries, "summary": summary}


def live_images(get, resolve_image=registry_image):
    """The image inputs of both Jaeger stacks as they run, for the Helmfile state values."""
    observed = snapshot(get, [], sorted(STACKS), resolve_image)["images"]
    images = {}
    for stack, ns in STACKS.items():
        images[stack] = {}
        for component, repo in REPOS.items():
            workload = "jaeger-hotrod" if component == "hotrod" else "jaeger"
            dep = get(f"/apis/apps/v1/namespaces/{ns}/deployments/{workload}")
            require(dep is not None, "Required Deployment is missing")
            containers = [c for c in dep["spec"]["template"]["spec"]["containers"] if c["name"] == workload]
            reference = observed[stack][component]["reference"]
            require(len(containers) == 1 and containers[0]["image"] == reference, "Deployment image changed while reading")
            match = re.fullmatch(r"(?:docker\.io/)?" + re.escape(repo) +
                                 r":([\w][\w.-]{0,127}(?:@sha256:[a-f0-9]{64})?)", reference, re.ASCII)
            policy = containers[0].get("imagePullPolicy")
            require(match and policy in ("Always", "IfNotPresent", "Never"), "Unexpected live image reference")
            images[stack][component] = {"repository": repo, "tag": match[1], "pullPolicy": policy}
    return images


def render_objects(root, temp, images):
    rendered = []

    def collect(_, ns, release, docs):
        rendered.extend((f"{ns}/{release}", obj) for obj in docs if obj is not None)

    pr_render_diff.render(root, temp, False, target=TARGET, index=collect, images=images)
    return rendered


def report(kubeconfig, context, root=ROOT, resolve_image=registry_image, open_reader=reader):
    revision = pr_render_diff.revision(root)
    with open_reader(kubeconfig, context) as get:
        images = live_images(get, resolve_image)
    with tempfile.TemporaryDirectory(prefix="demo-live-drift-") as staging:
        rendered = render_objects(root, Path(staging) / "render", images)
    with open_reader(kubeconfig, context) as get:
        routes, _ = discover(get)

        def namespaced(api, kind):
            route = routes.get((api, kind))
            require(route, "Unknown or ambiguous rendered kind")
            return route[2]

        items = plan(rendered, namespaced)
        check_scope(items)
        live = {key: get(item_path(key, routes)) for key in sorted(items)}
    require(pr_render_diff.revision(root) == revision, "Checkout changed while reporting")
    result = compare(items, live)
    result["renderInputs"] = {"revision": revision, "imagesSHA256": digest(canonical(images)),
                              "kubernetesTarget": " ".join(TARGET)}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    args = parser.parse_args(argv)
    try:
        result = report(args.kubeconfig, args.context)
    except (ValueError, KeyError, IndexError, TypeError, AttributeError, OSError, ArithmeticError,
            http.client.HTTPException, subprocess.SubprocessError, yaml.YAMLError) as error:
        # Never echo exception data. It can come from stored objects, Secrets or API error bodies.
        parser.exit(2, "Live drift report failed. Nothing was printed (" + type(error).__name__ + ").\n")
    print(json.dumps(result, indent=1, sort_keys=True))
    summary = result["summary"]
    return 1 if summary["unexplained"] or summary["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
