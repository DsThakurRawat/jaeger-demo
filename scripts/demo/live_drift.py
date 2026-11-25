#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Compare every object the Helmfile render would own with the live object.

The render uses the live Jaeger and HotROD image references of both stacks. Each
object is identical, explained (with reason codes from REASONS), unexplained or
missing. The report holds identities, classes, reason codes, field paths and
SHA-256 hashes. It never holds a field value, Secret or ConfigMap data, a map key
that only the live object has, a raw UID or an API error body. Exit status: 0 when
nothing is unexplained or missing, 1 when something is, 2 when the report could
not be produced.
"""

import argparse
import base64
from collections import namedtuple
import copy
import decimal
import hashlib
import json
from pathlib import Path
import re
import tempfile

import yaml

from deployer_access import MONITORING_CRDS
import pr_render_diff
from equivalence import TARGET
from live_observation import RELEASES, canonical, discover, item_path, object_state, reader, require, snapshot
from promotion_images import REPOS, STACKS, registry_image


ROOT = Path(__file__).resolve().parents[2]
COMPANION = "demo-resources"
CLASSES = ("identical", "explained", "unexplained", "missing")

# The only reasons a difference may be explained by. docs/helmfile-applications.md lists them.
REASONS = {
    "server-default": "Kubernetes API default for a field the render leaves unset, from a fixed list per kind",
    "server-quantity": "The API server's canonical form of a built-in resource quantity, equal in value to the render",
    "crd-schema-default": "Default declared in the rendered CRD schema for a custom resource field the render leaves "
                          "unset, under a parent the render sets",
    "helm-ownership": "Complete Helm ownership metadata of the expected release: the application release, "
                      "or demo-resources for an allowlisted companion",
    "companion-unadopted": "Allowlisted companion with no Helm ownership and no helm.sh/resource-policy, "
                           "which the ownership patch adds at adoption",
    "kubectl-last-applied": "kubectl apply annotation on an allowlisted companion, which the scripts create with kubectl apply",
    "service-allocation": "Allocated Service clusterIP, clusterIPs, ipFamilies or ipFamilyPolicy that the render leaves unset",
    "webhook-ca-bundle": "caBundle that the admission patch hook writes into the two monitoring webhook configurations",
    "deployment-revision": "deployment.kubernetes.io/revision annotation that the Deployment controller writes",
    "daemonset-generation": "deprecated.daemonset.template.generation annotation that the DaemonSet controller writes",
    "prometheus-operator-validated": "prometheus-operator-validated: \"true\" annotation that the monitoring chart's "
                                     "PrometheusRule mutating admission webhook adds",
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
# Map-typed fields of built-in kinds and object metadata. An entry under them is never
# omitted as empty, and a key only the live object has is not printed.
MAP_FIELDS = {"labels", "annotations", "data", "binaryData", "stringData", "nodeSelector", "matchLabels", "selector",
              "limits", "requests", "capacity", "allocatable", "overhead", "hard", "parameters", "volumeAttributes",
              "options"}
# Map keys the report may name although only the live object has them: they are fixed in this file.
KNOWN_KEYS = {"app.kubernetes.io/managed-by", "meta.helm.sh/release-name", "meta.helm.sh/release-namespace",
              "helm.sh/resource-policy", "kubectl.kubernetes.io/last-applied-configuration",
              "kubectl.kubernetes.io/restartedAt", "deployment.kubernetes.io/revision",
              "deprecated.daemonset.template.generation", "checksum/secret"}
LIVE_ONLY = "<live-only>"
# Non-pointer scalar fields of built-in kinds that the API server omits when they hold
# their zero value (false, 0 or ""). A rendered zero equals an absent live field there
# only. A pointer field, such as automountServiceAccountToken, keeps an explicit false.
OMITTED_ZERO = {("hostNetwork",), ("hostPID",), ("hostIPC",), ("stdin",), ("stdinOnce",), ("tty",),
                ("volumeMounts", "[]", "readOnly"), ("spec", "paused"), ("publishNotReadyAddresses",),
                ("minReadySeconds",), ("initialDelaySeconds",), ("hostPort",), ("nodePort",), ("subPath",),
                ("subPathExpr",), ("workingDir",), ("hostname",), ("subdomain",), ("nodeName",),
                ("priorityClassName",), ("serviceAccountName",), ("env", "[]", "value")}

# One rule: a reason, the apiVersion:Kind values it covers, a path pattern ("[]" is
# any list index, "*" any map key), the expected live value (a constant, ANY, or a
# function of the live parent object), an optional predicate on the context, and
# whether a rendered zero value counts as unset. That holds only for a non-pointer
# field that the API server defaults when it is zero.
Rule = namedtuple("Rule", "reason kinds pattern value where zero", defaults=(None, False))
Z = True  # marks a default the API server also applies to a rendered zero value
POD_PREFIXES = {"apps/v1:Deployment": ("spec", "template", "spec"), "apps/v1:StatefulSet": ("spec", "template", "spec"),
                "apps/v1:DaemonSet": ("spec", "template", "spec"), "batch/v1:Job": ("spec", "template", "spec"),
                "batch/v1:CronJob": ("spec", "jobTemplate", "spec", "template", "spec")}


def _pull_policy(container):
    image = container.get("image", "")
    if "@" in image:
        return "IfNotPresent"
    last = image.rsplit("/", 1)[-1]
    return "Always" if ":" not in last or last.rsplit(":", 1)[1] == "latest" else "IfNotPresent"


def _subject_group(subject):
    return "rbac.authorization.k8s.io" if subject.get("kind") in ("User", "Group") else ""


def _server_defaults():
    pod = [(("dnsPolicy",), "ClusterFirst", Z), (("restartPolicy",), "Always", Z),
           (("schedulerName",), "default-scheduler", Z), (("securityContext",), {}, False),
           (("terminationGracePeriodSeconds",), 30, False),
           # The API server mirrors serviceAccountName into the deprecated field.
           (("serviceAccount",), lambda pod: pod.get("serviceAccountName"), Z)]
    container = [(("terminationMessagePath",), "/dev/termination-log", Z), (("terminationMessagePolicy",), "File", Z),
                 (("imagePullPolicy",), _pull_policy, Z), (("resources",), {}, False),
                 (("ports", "[]", "protocol"), "TCP", Z), (("env", "[]", "valueFrom", "fieldRef", "apiVersion"), "v1", Z)]
    for probe in ("livenessProbe", "readinessProbe", "startupProbe"):
        container += [((probe, "timeoutSeconds"), 1, Z), ((probe, "periodSeconds"), 10, Z),
                      ((probe, "successThreshold"), 1, Z), ((probe, "failureThreshold"), 3, Z),
                      ((probe, "httpGet", "scheme"), "HTTP", Z), ((probe, "httpGet", "path"), "/", Z),
                      ((probe, "grpc", "service"), "", False)]
    for hook in ("postStart", "preStop"):
        container += [(("lifecycle", hook, "httpGet", "scheme"), "HTTP", Z), (("lifecycle", hook, "httpGet", "path"), "/", Z)]
    volume = [(("configMap", "defaultMode"), 420, False), (("secret", "defaultMode"), 420, False),
              (("projected", "defaultMode"), 420, False), (("downwardAPI", "defaultMode"), 420, False),
              (("hostPath", "type"), "", False), (("downwardAPI", "items", "[]", "fieldRef", "apiVersion"), "v1", Z),
              (("projected", "sources", "[]", "downwardAPI", "items", "[]", "fieldRef", "apiVersion"), "v1", Z),
              (("projected", "sources", "[]", "serviceAccountToken", "expirationSeconds"), 3600, False)]
    rules = []
    for kind, prefix in POD_PREFIXES.items():
        rules += [Rule("server-default", {kind}, prefix + path, value, None, zero) for path, value, zero in pod]
        for field in ("containers", "initContainers"):
            rules += [Rule("server-default", {kind}, prefix + (field, "[]") + path, value, None, zero)
                      for path, value, zero in container]
        rules += [Rule("server-default", {kind}, prefix + ("volumes", "[]") + path, value, None, zero)
                  for path, value, zero in volume]
        # An unset pod template metadata reads back as an empty object.
        rules.append(Rule("server-default", {kind}, prefix[:-1] + ("metadata",), {}))
    rules.append(Rule("server-default", {"batch/v1:CronJob"}, ("spec", "jobTemplate", "metadata"), {}))
    workload = {
        "apps/v1:Deployment": [(("replicas",), 1, False), (("revisionHistoryLimit",), 10, False),
                               (("progressDeadlineSeconds",), 600, False), (("strategy", "type"), "RollingUpdate", Z),
                               (("strategy", "rollingUpdate", "maxSurge"), "25%", False),
                               (("strategy", "rollingUpdate", "maxUnavailable"), "25%", False)],
        "apps/v1:StatefulSet": [(("replicas",), 1, False), (("revisionHistoryLimit",), 10, False),
                                (("podManagementPolicy",), "OrderedReady", Z), (("updateStrategy", "type"), "RollingUpdate", Z),
                                (("updateStrategy", "rollingUpdate", "partition"), 0, False),
                                (("updateStrategy", "rollingUpdate", "maxUnavailable"), 1, False),
                                (("persistentVolumeClaimRetentionPolicy", "whenDeleted"), "Retain", Z),
                                (("persistentVolumeClaimRetentionPolicy", "whenScaled"), "Retain", Z),
                                (("volumeClaimTemplates", "[]", "apiVersion"), "v1", Z),
                                (("volumeClaimTemplates", "[]", "kind"), "PersistentVolumeClaim", Z),
                                (("volumeClaimTemplates", "[]", "spec", "volumeMode"), "Filesystem", False),
                                (("volumeClaimTemplates", "[]", "status", "phase"), "Pending", Z)],
        "apps/v1:DaemonSet": [(("revisionHistoryLimit",), 10, False), (("updateStrategy", "type"), "RollingUpdate", Z),
                              (("updateStrategy", "rollingUpdate", "maxUnavailable"), 1, False),
                              (("updateStrategy", "rollingUpdate", "maxSurge"), 0, False)],
        "batch/v1:CronJob": [(("concurrencyPolicy",), "Allow", Z), (("suspend",), False, False),
                             (("successfulJobsHistoryLimit",), 3, False), (("failedJobsHistoryLimit",), 1, False)],
        "batch/v1:Job": [(("backoffLimit",), 6, False), (("completions",), 1, False), (("parallelism",), 1, False),
                         (("completionMode",), "NonIndexed", False), (("suspend",), False, False),
                         (("manualSelector",), False, False), (("podReplacementPolicy",), "TerminatingOrFailed", False)],
        "v1:Service": [(("type",), "ClusterIP", Z), (("sessionAffinity",), "None", Z),
                       (("internalTrafficPolicy",), "Cluster", False), (("ports", "[]", "protocol"), "TCP", Z),
                       (("ports", "[]", "targetPort"), lambda port: port.get("port"), Z)],
        "apiextensions.k8s.io/v1:CustomResourceDefinition": [(("conversion", "strategy"), "None", Z)],
    }
    for kind, fields in workload.items():
        rules += [Rule("server-default", {kind}, ("spec",) + path, value, None, zero) for path, value, zero in fields]
    # clusterIPs mirrors a rendered clusterIP such as None. An allocated address is service-allocation.
    rules.append(Rule("server-default", {"v1:Service"}, ("spec", "clusterIPs"), lambda spec: [spec.get("clusterIP")],
                      lambda ctx: at(ctx["render"], ("spec", "clusterIP")) is not ABSENT))
    rules.append(Rule("server-default", {"v1:Secret"}, ("type",), "Opaque", None, Z))
    bindings = {"rbac.authorization.k8s.io/v1:RoleBinding", "rbac.authorization.k8s.io/v1:ClusterRoleBinding"}
    rules.append(Rule("server-default", bindings, ("subjects", "[]", "apiGroup"), _subject_group, None, Z))
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
    # The monitoring chart's prometheusrulemutate webhook adds this exact value to every PrometheusRule it admits.
    Rule("prometheus-operator-validated", {"monitoring.coreos.com/v1:PrometheusRule"},
         ("metadata", "annotations", "prometheus-operator-validated"), "true"),
    Rule("rollout-restart", {"apps/v1:Deployment"},
         ("spec", "template", "metadata", "annotations", "kubectl.kubernetes.io/restartedAt"), ANY,
         lambda ctx: ctx["identity"] in RESTARTED),
    Rule("kubectl-last-applied", None, ("metadata", "annotations", "kubectl.kubernetes.io/last-applied-configuration"),
         ANY, _companion),
)


def _quantity_paths():
    paths = []
    for kind, prefix in POD_PREFIXES.items():
        for field in ("containers", "initContainers"):
            paths += [({kind}, prefix + (field, "[]", "resources", group, "*")) for group in ("limits", "requests")]
        paths += [({kind}, prefix + ("resources", group, "*")) for group in ("limits", "requests")]
        paths.append(({kind}, prefix + ("volumes", "[]", "emptyDir", "sizeLimit")))
    paths += [({"apps/v1:StatefulSet"}, ("spec", "volumeClaimTemplates", "[]", "spec", "resources", group, "*"))
              for group in ("limits", "requests")]
    paths += [({"v1:PersistentVolumeClaim"}, ("spec", "resources", group, "*")) for group in ("limits", "requests")]
    return tuple(paths)


# The built-in fields that hold a resource quantity, which the API server stores in canonical form.
QUANTITY_PATHS = _quantity_paths()
QUANTITY = re.compile(r"([+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+))([eE][+-]?[0-9]+|[KMGTPE]i|[numkMGTPE]?)")
SUFFIX = {"": 1, "n": decimal.Decimal("1e-9"), "u": decimal.Decimal("1e-6"), "m": decimal.Decimal("1e-3"),
          "k": 10 ** 3, "M": 10 ** 6, "G": 10 ** 9, "T": 10 ** 12, "P": 10 ** 15, "E": 10 ** 18,
          "Ki": 2 ** 10, "Mi": 2 ** 20, "Gi": 2 ** 30, "Ti": 2 ** 40, "Pi": 2 ** 50, "Ei": 2 ** 60}


def quantity(value):
    """The value of a Kubernetes quantity, or None when it is not one."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    match = QUANTITY.fullmatch(str(value))
    if not match:
        return None
    number, suffix = decimal.Decimal(match[1]), match[2]
    if re.fullmatch(r"[eE][+-]?[0-9]+", suffix):
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


def tail(patterns, path):
    return any(len(path) >= len(p) and matches(p, path[-len(p):]) for p in patterns)


def same(a, b):
    if a is ABSENT or b is ABSENT:
        return a is b
    return canonical(a) == canonical(b)


def builtin_api(api):
    """Built-in API groups: core, a group without a dot (apps, batch, policy) or a *.k8s.io group."""
    group = api.rpartition("/")[0]
    return not group or "." not in group or group.endswith(".k8s.io")


SCHEMA_CONTAINERS = {"properties", "patternProperties", "definitions", "dependencies"}


def schema_payload(path):
    """Whether `path` is inside a CRD schema's JSON value: a `default`, an `example` or an `enum` entry.

    The API server keeps such a value as given, so an empty object or list there is data, not an
    omitted field. A property that happens to be named `default` is a schema, not a value.
    """
    if "openAPIV3Schema" not in path:
        return False
    start = path.index("openAPIV3Schema")
    for index in range(start + 1, len(path)):
        key, parent = path[index], path[index - 1]
        if parent in SCHEMA_CONTAINERS:
            continue
        if key in ("default", "example") or (key == "enum" and index < len(path) - 1):
            return True
    return False


# Kinds whose spec.selector is a plain label map. Elsewhere `selector` is a LabelSelector, where an
# empty selector selects everything and an absent one selects nothing.
MAP_SELECTOR_KINDS = {"v1:Service", "v1:ReplicationController"}


def map_typed(path, kind):
    """Whether the field at `path` is map-typed, which Go omits when empty, rather than a struct."""
    if not path or path[-1] not in MAP_FIELDS | SCHEMA_CONTAINERS:
        return False
    if path[-1] == "selector":
        return kind in MAP_SELECTOR_KINDS and path == ("spec", "selector")
    if path[-1] in SCHEMA_CONTAINERS and path[-1] not in MAP_FIELDS:
        return "openAPIV3Schema" in path
    return True


def omitted(path, render, builtin, kind=""):
    """Whether the API server omits this rendered value, so that an absent live field equals it."""
    if not (builtin or path[:1] == ("metadata",)) or (len(path) >= 2 and map_typed(path[:-1], kind)):
        return False  # a custom resource keeps what it is given; a map keeps every entry
    if schema_payload(path):
        return False  # a CRD schema default, example or enum value is kept even when empty
    if isinstance(render, dict):
        # An empty map is omitted. An empty struct, such as a LabelSelector, is kept as {}.
        return not render and map_typed(path, kind)
    if isinstance(render, list):
        return not render
    return not isinstance(render, (dict, list)) and not render and tail(OMITTED_ZERO, path)


def differences(render, live, builtin, path=(), kind=""):
    """Yield (path, render value, live value) for every leaf that differs."""
    if isinstance(render, dict) and isinstance(live, dict):
        for key in sorted(render.keys() | live.keys()):
            yield from differences(render.get(key, ABSENT), live.get(key, ABSENT), builtin, path + (key,), kind)
    elif render is ABSENT and isinstance(live, dict) and live:
        for key in sorted(live):
            yield from differences(ABSENT, live[key], builtin, path + (key,), kind)
    elif live is ABSENT and isinstance(render, dict) and render:
        for key in sorted(render):
            yield from differences(render[key], ABSENT, builtin, path + (key,), kind)
    elif isinstance(render, list) and isinstance(live, list) and len(render) == len(live):
        for index, (r, v) in enumerate(zip(render, live)):
            yield from differences(r, v, builtin, path + (index,), kind)
    elif live is ABSENT and render is not ABSENT and omitted(path, render, builtin, kind):
        return
    elif render is ABSENT or live is ABSENT or not same(render, live):
        yield path, render, live


def crd_scopes(rendered):
    """(apiVersion, kind) to namespaced, for every version of every rendered CRD."""
    scopes = {}
    for _, obj in rendered:
        if obj.get("kind") == "CustomResourceDefinition" and obj.get("apiVersion") == "apiextensions.k8s.io/v1":
            spec = obj["spec"]
            for version in spec["versions"]:
                scopes[(spec["group"] + "/" + version["name"], spec["names"]["kind"])] = spec["scope"] == "Namespaced"
    return scopes


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


def _schema_node(schema, path):
    node = schema
    for key in path:
        if not isinstance(node, dict):
            return None
        if isinstance(key, int):
            node = node.get("items")
        elif key in node.get("properties", {}):
            node = node["properties"][key]
        elif isinstance(node.get("additionalProperties"), dict):
            node = node["additionalProperties"]
        else:
            return None
    return node if isinstance(node, dict) else None


def _schema_default(schema, path):
    node = _schema_node(schema, path)
    return node.get("default", ABSENT) if node else ABSENT


def _defaulted(schema, render, path):
    """The API server applies a schema default only under a parent that exists or is itself defaulted."""
    parent = path[:-1]
    if not parent or at(render, parent) is not ABSENT:
        return True
    if isinstance(parent[-1], int):
        return False
    return _schema_default(schema, parent) is not ABSENT and _defaulted(schema, render, parent)


def _merge(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def grafana_uses_lookup(root=ROOT):
    """Whether the checkout leaves the Grafana admin password to the chart's lookup of the live Secret.

    The offline render sets a public fixture password, which hides the checkout's own
    choice. Read the release's values: anything that may set the password, or that
    this cannot read (a template, set or secrets), counts as an explicit password.
    """
    state = yaml.safe_load((root / "helmfile.yaml").read_text())
    found = [r for r in state.get("releases", []) if (r.get("namespace"), r.get("name")) == ("default", "prometheus")]
    if len(found) != 1 or any(found[0].get(key) for key in ("set", "setString", "secrets", "valuesTemplate")):
        return False
    values = {}
    for entry in found[0].get("values", []):
        if isinstance(entry, str):
            if not entry.endswith((".yaml", ".yml")):
                return False
            entry = yaml.safe_load((root / entry).read_text()) or {}
        if not isinstance(entry, dict):
            return False
        _merge(values, entry)
    grafana = values.get("grafana") or {}
    env = grafana.get("env") or {}
    return not (grafana.get("enabled") is False or grafana.get("adminPassword")
                or (grafana.get("admin") or {}).get("existingSecret")
                or any(key in env for key in ("GF_SECURITY_ADMIN_PASSWORD", "GF_SECURITY_ADMIN_PASSWORD__FILE",
                                              "GF_SECURITY_DISABLE_INITIAL_ADMIN_CREATION")))


def grafana_checksum(data, password, quoted):
    """sha256sum of the grafana.secretsData template of the pinned Grafana chart."""
    value = f'"{password}"' if quoted else password
    text = f'\nadmin-user: "{data["admin-user"]}"\nadmin-password: {value}\nldap-toml: "{data["ldap-toml"]}"'
    return digest(text.encode())


def _grafana(ctx, path, render, live):
    if not ctx["grafana_lookup"]:
        return False  # the checkout chooses the password; the fixture password proves nothing
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


def _allocated(ctx, path, live):
    """An address or family the API server allocated. It never allocates a headless None."""
    if path not in ALLOCATION:
        return False
    if path == ("spec", "clusterIP"):
        return isinstance(live, str) and live not in ("", "None")
    if path == ("spec", "clusterIPs"):
        # The first entry is always the clusterIP itself.
        return (isinstance(live, list) and bool(live) and live[0] == at(ctx["normalized"], ("spec", "clusterIP"))
                and all(isinstance(ip, str) and ip not in ("", "None") for ip in live))
    return True


def explain(ctx, path, render, live):
    """The reason code that explains one leaf difference, or None."""
    kind = ctx["kind"]
    if _grafana(ctx, path, render, live):
        return "grafana-admin-password"
    # A rendered zero counts as unset only for a rule that says the API server treats it so.
    zero = (ctx["builtin"] and render is not ABSENT and not isinstance(render, (bool, dict, list)) and not render)
    if (render is ABSENT or zero) and live is not ABSENT:
        if kind == "v1:Service" and render is ABSENT and _allocated(ctx, path, live):
            return "service-allocation"
        for rule in RULES:
            if ((rule.kinds is None or kind in rule.kinds) and matches(rule.pattern, path)
                    and (render is ABSENT or rule.zero) and (rule.where is None or rule.where(ctx))):
                if rule.value is ANY:
                    if render is ABSENT and isinstance(live, str):
                        return rule.reason
                    continue
                expected = rule.value(at(ctx["normalized"], path[:-1])) if callable(rule.value) else rule.value
                if same(expected, live):
                    return rule.reason
        schema = ctx["schemas"].get((ctx["api"], ctx["kind_name"]))
        if render is ABSENT and not ctx["builtin"] and schema is not None and path[0] != "metadata":
            default = _schema_default(schema, path)
            if default is not ABSENT and same(default, live) and _defaulted(schema, ctx["render"], path):
                return "crd-schema-default"
    if (ctx["builtin"] and render is not ABSENT and isinstance(live, str)
            and any(kind in kinds and matches(pattern, path) for kinds, pattern in QUANTITY_PATHS)):
        a, b = quantity(render), quantity(live)
        if a is not None and a == b:
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


def _map_key(ctx, prefix, key):
    """Whether `key` under `prefix` is a map key (named by data), not a fixed field name."""
    if prefix and prefix[-1] in MAP_FIELDS:
        return True
    if (ctx["kind"] == "apiextensions.k8s.io/v1:CustomResourceDefinition" and len(prefix) >= 4
            and prefix[:2] == ("spec", "versions") and prefix[3] == "schema"):
        return True  # a schema names properties and holds default values: data, not fixed field names
    if ctx["builtin"] or prefix[:1] == ("metadata",):
        return False
    node = _schema_node(ctx["schemas"].get((ctx["api"], ctx["kind_name"])), prefix)
    return not (node and key in node.get("properties", {}))  # unknown counts as a map key


def field(ctx, path):
    """A printable field path: no Secret or ConfigMap key, and no map key that only the live object has."""
    if ctx["kind"] in ("v1:Secret", "v1:ConfigMap") and path[:1] in (("data",), ("binaryData",), ("stringData",)):
        path = path[:1]
    parts, node = [], ctx["render"]
    for index, key in enumerate(path):
        if (isinstance(key, str) and key not in KNOWN_KEYS and not (isinstance(node, dict) and key in node)
                and _map_key(ctx, path[:index], key)):
            parts.append(LIVE_ONLY)
            break
        parts.append(key)
        node = at(node, (key,)) if node is not ABSENT else ABSENT
    return "".join(f"[{p}]" if isinstance(p, int) else ("." if i else "") + p for i, p in enumerate(parts))


def classify(key, items, live, schemas, grafana_lookup=False):
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
           "kind": api + ":" + kind_name, "builtin": builtin_api(api), "schemas": schemas, "items": items,
           "live": live, "normalized": current, "render": render, "grafana_lookup": grafana_lookup}
    reasons, unexplained = _ownership(ctx, render, current)
    for path, r, v in differences(render, current, ctx["builtin"], kind=ctx["kind"]):
        reason = explain(ctx, path, r, v)
        if reason:
            reasons.add(reason)
        else:
            unexplained.append(field(ctx, path))
    entry["reasons"] = sorted(reasons)
    if unexplained:
        entry.update(unexplainedFields=sorted(set(unexplained)), **{"class": "unexplained"})
    else:
        entry["class"] = "explained" if reasons else "identical"
    return entry


def compare(items, live, grafana_lookup=False):
    """Classify each planned identity against `live`, a map from identity to object or None.

    `grafana_lookup` is grafana_uses_lookup() for the checkout that rendered `items`.
    """
    schemas = {k: v for k, v in _schemas(items).items() if not builtin_api(k[0])}
    entries = [classify(key, items, live, schemas, grafana_lookup) for key in sorted(items)]
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


def render_objects(root, temp, images, grafana_lookup):
    """The Helmfile render. The fixture Grafana password stands in for the chart's lookup only when
    the checkout leaves the password to that lookup; otherwise the checkout's own values apply."""
    rendered = []

    def collect(_, ns, release, docs):
        rendered.extend((f"{ns}/{release}", obj) for obj in docs if obj is not None)

    pr_render_diff.render(root, temp, False, target=TARGET, index=collect, images=images, stable=grafana_lookup)
    return rendered


def read_live(get, rendered):
    """Plan the render against server discovery and read each identity with a named GET.

    A kind whose CRD is absent from the cluster takes its scope from the rendered CRD;
    its objects are missing and are not read. Ambiguous discovery stops the report.
    """
    routes, _ = discover(get)
    scopes = crd_scopes(rendered)

    def namespaced(api, kind):
        if (api, kind) in routes:
            route = routes[(api, kind)]
            require(route, "Ambiguous rendered kind")
            return route[2]
        require((api, kind) in scopes, "Unknown rendered kind")
        return scopes[(api, kind)]

    items = plan(rendered, namespaced)
    live = {key: get(item_path(key, routes)) if routes.get(tuple(key.split(":")[:2])) else None
            for key in sorted(items)}
    return items, live


def report(kubeconfig, context, root=ROOT, resolve_image=registry_image, open_reader=reader):
    revision = pr_render_diff.revision(root)
    lookup = grafana_uses_lookup(root)
    with open_reader(kubeconfig, context) as get:
        images = live_images(get, resolve_image)
    with tempfile.TemporaryDirectory(prefix="demo-live-drift-") as staging:
        rendered = render_objects(root, Path(staging) / "render", images, lookup)
    with open_reader(kubeconfig, context) as get:
        items, live = read_live(get, rendered)
    check_scope(items)
    require(pr_render_diff.revision(root) == revision, "Checkout changed while reporting")
    result = compare(items, live, lookup)
    result["renderInputs"] = {"revision": revision, "imagesSHA256": digest(canonical(images)),
                              "kubernetesTarget": " ".join(TARGET)}
    return result


def main(argv=None, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--output", type=Path, help="write the report to this file, outside the repository checkout")
    args = parser.parse_args(argv)
    if args.output:
        target = args.output.expanduser().resolve()
        checkout = root.resolve()
        if target == checkout or checkout in target.parents:
            # An untracked file in the checkout makes the next report refuse the checkout.
            parser.error("--output must be outside the repository checkout")
    try:
        result = report(args.kubeconfig, args.context, root)
        text = json.dumps(result, indent=1, sort_keys=True) + "\n"
        if args.output:
            args.output.expanduser().write_text(text)
    except Exception as error:  # noqa: BLE001
        # Never echo exception data. It can come from stored objects, Secrets or API error bodies.
        parser.exit(2, "Live drift report failed. Nothing was printed (" + type(error).__name__ + ").\n")
    if not args.output:
        print(text, end="")
    summary = result["summary"]
    return 1 if summary["unexplained"] or summary["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
