# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Prepare and verify the existing six Helm releases for the scripts-v1 daily request."""

from base64 import b64decode
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tempfile
import time
import zlib

import yaml

import deploy
import stack_health
import verify_trace


ROOT = Path(__file__).resolve().parents[2]
RELEASES = {
    "oci-monitoring": ("default", "prometheus", 1200), "oci-jaeger": ("default", "jaeger", 600),
    "opensearch": ("opensearch", "opensearch", 600),
    "opensearch-dashboards": ("opensearch", "opensearch-dashboards", 3600),
    "otel-jaeger": ("jaeger", "jaeger", 600), "otel-demo": ("otel-demo", "otel-demo", 900),
}
COMPONENTS = tuple(k for k in RELEASES if k != "opensearch-dashboards")
SUPPLEMENTS = {
    "oci-monitoring": (("default", "oci/prometheus-svc.yaml"),),
    "oci-jaeger": (("default", "oci/load-generator/load-generator.yaml"), ("default", "oci/ingress.yaml")),
    "otel-jaeger": (("jaeger", "otel-demo/jaeger-query-service.yaml"), ("jaeger", "otel-demo/load-generator.yaml"),
                   ("jaeger", "otel-demo/ingress/ingress-jaeger.yaml"), ("opensearch", "otel-demo/ingress/ingress-opensearch.yaml"),
                   ("otel-demo", "otel-demo/ingress/ingress-otel-demo.yaml")),
}
VALUES = {
    "oci-monitoring": "oci/monitoring-values.yaml", "oci-jaeger": "oci/jaeger-values.yaml",
    "opensearch": "otel-demo/opensearch-values.yaml", "opensearch-dashboards": "otel-demo/opensearch-dashboard-values.yaml",
    "otel-jaeger": "otel-demo/jaeger-values.yaml", "otel-demo": "otel-demo/otel-demo-values.yaml",
}
CLUSTER = {"ClusterRole", "ClusterRoleBinding", "ValidatingWebhookConfiguration", "MutatingWebhookConfiguration", "CustomResourceDefinition"}
PROTECTED = CLUSTER | {"Role", "RoleBinding", "Secret", "ServiceAccount", "Prometheus", "Alertmanager"}
HELM_HOOK = "helm.sh/hook"
HEX64 = re.compile(r"[0-9a-f]{64}")
HEX40 = re.compile(r"[0-9a-f]{40}")
LIMIT = 64 * 1024 * 1024
CHARTS = {"oci-monitoring": "kube-prometheus-stack", "oci-jaeger": "jaeger", "otel-jaeger": "jaeger",
          "opensearch": "opensearch", "opensearch-dashboards": "opensearch-dashboards", "otel-demo": "opentelemetry-demo"}
MEMBERS = {"plan.json", *("charts/" + k + ".tgz" for k in RELEASES), *("values/" + k + ".json" for k in RELEASES),
           *("renders/" + k + ".json" for k in RELEASES), *("supplemental/" + k + ".json" for k in COMPONENTS)}


class AdapterError(RuntimeError):
    """Fixed diagnostics only. Cluster, Helm and YAML responses can contain Secrets."""


def need(ok, message):
    if not ok:
        raise AdapterError(message)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def component(key):
    return "opensearch" if key == "opensearch-dashboards" else key


class Loader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    pass


Loader.add_constructor("tag:yaml.org,2002:value", Loader.construct_scalar)


def docs(raw):
    return [obj for obj in yaml.load_all(raw, Loader=Loader) if obj is not None]


def identity(obj, namespace):
    api, kind, meta = obj["apiVersion"], obj["kind"], obj["metadata"]
    ns = "-" if kind in CLUSTER else meta.get("namespace", namespace)
    parts = (api, kind, ns, meta["name"])
    need(all(isinstance(p, str) and re.fullmatch(r"[A-Za-z0-9./-]+", p) for p in parts), "Invalid rendered identity")
    need(ns != "-" or kind in CLUSTER, "Ambiguous resource scope")
    return ":".join(parts)


def index(objs, ns):
    by_id = {}
    for obj in objs:
        # Helm 4 annotates the entire stream for post-rendering and removes its
        # filename afterwards. Null, empty and omitted annotation maps are the
        # same Kubernetes metadata and can be encoded differently on readback.
        meta = obj["metadata"]
        if not meta.get("annotations"):
            obj = dict(obj, metadata={k: v for k, v in meta.items() if k != "annotations"})
        by_id[identity(obj, ns)] = obj
    need(len(by_id) == len(objs), "Duplicate rendered object")
    return by_id


def release_docs(release):
    return docs(release["manifest"]) + [obj for h in release.get("hooks") or [] for obj in docs(h["manifest"])]


def image_references(obj):
    refs = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "image" and isinstance(v, str):
                refs.add(v)
            elif isinstance(v, (list, dict)):
                refs.update(image_references(v))
    elif isinstance(obj, list):
        for item in obj:
            refs.update(image_references(item))
    return refs


def override(values, path, value):
    for key in path[:-1]:
        values = values.setdefault(key, {})
    values[path[-1]] = value


def values_for(key, root, images):
    examples = root / "examples"
    data = yaml.safe_load((examples / VALUES[key]).read_text())
    if key == "oci-monitoring":
        for leaf in ("enabled", "forceConflicts"):
            override(data, ("crds", "upgradeJob", leaf), True)
    if key.endswith("-jaeger"):
        oci = key == "oci-jaeger"
        data["userconfig"] = (examples / ("oci/config.yaml" if oci else "otel-demo/jaeger-config.yaml")).read_text()
        data["uiconfig"] = (examples / "oci/ui-config.json").read_text()
        override(data, ("allInOne", "enabled"), True)
        if oci:
            override(data, ("provisionDataStore", "cassandra"), False)
        else:
            for field, value in {"anonymous": True, "usePassword": False, "port": 9200,
                                 "host": "opensearch-cluster-single.opensearch.svc.cluster.local"}.items():
                override(data, ("storage", "elasticsearch", field), value)
        override(data, ("storage", "type"), "memory" if oci else "elasticsearch")
        for image, prefix in (("jaeger", "allInOne"), ("hotrod", "hotrod")):
            selected = images[image]
            repo = selected["repository"].removeprefix("docker.io/")
            override(data, (prefix, "image", "repository"), repo)
            override(data, (prefix, "image", "tag"), selected["tag"] + "@" + selected["digest"])
            override(data, (prefix, "image", "pullPolicy"), "Always")
    return data


def supplemental_for(key, root):
    examples = root / "examples"
    result = []
    for ns, filename in SUPPLEMENTS.get(key, ()):
        for obj in docs((examples / filename).read_bytes()):
            need(obj["metadata"].setdefault("namespace", ns) == ns, "Supplemental namespace changed")
            result.append(obj)
    if key.endswith("-jaeger"):
        ns = RELEASES[key][0]
        parent = examples / ("oci/load-generator" if ns == "default" else "otel-demo")
        requirements = parent / ("requirements.txt" if ns == "default" else "load-generator/requirements.txt")
        result.insert(0, {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"namespace": ns, "name": "trace-script"},
                          "data": {"generate_traces.py": (parent / "generate_traces.py").read_text(),
                                   "requirements.txt": requirements.read_text()}})
    return result


def images_valid(images):
    need(set(images) == {"jaeger", "hotrod"}, "Missing independently resolved images")
    for key, repo in (("jaeger", "jaegertracing/jaeger-snapshot"), ("hotrod", "jaegertracing/example-hotrod-snapshot")):
        src = images[key]
        need(src["repository"].removeprefix("docker.io/") == repo and HEX40.fullmatch(src["sourceCommit"])
             and src["tag"] == src["sourceCommit"] and all(re.fullmatch("sha256:[0-9a-f]{64}", src[k]) for k in ("digest", "platformManifest"))
             and src["reference"] == src["repository"] + ":" + src["tag"] + "@" + src["digest"], "Invalid snapshot provenance")


def validate_change(key, observed, candidate, images):
    ns = RELEASES[key][0]
    old, new = index(observed, ns), index(candidate, ns)
    need(old.keys() == new.keys(), "Daily upgrades require an approved existing object inventory")
    for ident, obj in new.items():
        prior = old[ident]
        kind, name = obj["kind"], obj["metadata"]["name"]
        protected = kind in PROTECTED or HELM_HOOK in (obj["metadata"].get("annotations") or {}) or HELM_HOOK in (prior["metadata"].get("annotations") or {})
        if protected or key == "oci-monitoring" and kind in {"Deployment", "DaemonSet", "StatefulSet"}:
            need(encode(prior) == encode(obj), "Privileged manifest differs from the operator-established baseline")
        if key == "opensearch" and kind == "StatefulSet":
            for field in ("volumeClaimTemplates", "persistentVolumeClaimRetentionPolicy", "selector", "serviceName"):
                need(obj["spec"].get(field) == prior["spec"].get(field), "OpenSearch storage identity change requires an operator")
            need(obj["spec"].get("replicas", 1) == prior["spec"].get("replicas", 1) == 1,
                 "OpenSearch topology changes require an operator")
        if key.endswith("-jaeger") and kind == "Deployment" and name in {"jaeger", "jaeger-hotrod"}:
            wanted = images["jaeger" if name == "jaeger" else "hotrod"]["reference"].removeprefix("docker.io/")
            found = [c["image"].removeprefix("docker.io/") for c in obj["spec"]["template"]["spec"]["containers"] if c["name"] == name]
            need(found == [wanted], "Frozen render does not use the resolved snapshot image")
    if key.endswith("-jaeger"):
        need(all("apps/v1:Deployment:" + ns + ":" + n in new for n in ("jaeger", "jaeger-hotrod")), "Missing snapshot deployments")
    if key == "oci-monitoring":
        hooks = [o for o in new.values() if o["kind"] == "Job" and HELM_HOOK in (o["metadata"].get("annotations") or {})]
        need(len(hooks) == 3 and all(image_references(o) and all(re.search(r"@sha256:[a-f0-9]{64}$", r)
             for r in image_references(o)) for o in hooks), "Monitoring baseline lacks all three digest-pinned hook Jobs")


def command(args, *, body=None, timeout=90, env=None):
    # kubectl and Helm can print entire Secrets. Bound stdout in RAM while it
    # is being read and discard stderr. A timeout or oversized stream stops
    # the whole command group, including a spawned post-renderer.
    try:
        with subprocess.Popen(args, stdin=subprocess.PIPE if body else subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, start_new_session=True) as p:
            try:
                output = bytearray()
                deadline = time.monotonic() + timeout
                with selectors.DefaultSelector() as selector:
                    os.set_blocking(p.stdout.fileno(), False)
                    selector.register(p.stdout, selectors.EVENT_READ)
                    if body:
                        pending = memoryview(body)
                        os.set_blocking(p.stdin.fileno(), False)
                        selector.register(p.stdin, selectors.EVENT_WRITE)
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        need(remaining > 0, "Scheduled command timed out")
                        events = selector.select(remaining)
                        need(events, "Scheduled command timed out")
                        for handle, event in events:
                            stream = handle.fileobj
                            if event == selectors.EVENT_READ:
                                chunk = os.read(stream.fileno(), 65536)
                                if chunk:
                                    need(len(output) + len(chunk) <= LIMIT, "Oversized scheduled command output")
                                    output.extend(chunk)
                                    continue
                            else:
                                try:
                                    pending = pending[os.write(stream.fileno(), pending[:65536]):]
                                    if pending:
                                        continue
                                except BrokenPipeError:
                                    pass
                            selector.unregister(stream)
                            stream.close()
                need(p.wait(timeout=max(0, deadline - time.monotonic())) == 0, "Scheduled command failed")
                return bytes(output)
            finally:
                if p.poll() is None:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
    except (OSError, subprocess.TimeoutExpired):
        raise AdapterError("Scheduled command unavailable or timed out") from None


def kube(*args, body=None, timeout=90):
    return command(["kubectl", "--request-timeout=" + str(timeout) + "s", *args], body=body, timeout=timeout + 10)


def get_object(ident):
    api, kind, ns, name = ident.split(":")
    meta = {"name": name}
    if ns != "-":
        meta["namespace"] = ns
    obj = json.loads(kube("get", "-f", "-", "-o", "json", body=encode({"apiVersion": api, "kind": kind, "metadata": meta})))
    if obj["apiVersion"] == "v1" and obj["kind"] == "List":
        need(len(obj["items"]) == 1, "Expected exactly one named resource")
        obj = obj["items"][0]
    need(identity(obj, ns) == ident and obj["metadata"].get("uid") and not obj["metadata"].get("deletionTimestamp"),
         "Expected an active named object")
    return obj


def projection(obj):
    meta = {k: v for k, v in obj["metadata"].items() if k not in {"managedFields", "resourceVersion", "generation", "creationTimestamp"}}
    core = {k: v for k, v in obj.items() if k not in {"status", "metadata"}}
    state = {"uid": meta["uid"], "state": sha(encode(dict(core, metadata=meta)))}
    if obj["kind"] == "Service":
        state["binding"] = {k: obj["spec"].get(k) for k in ("clusterIP", "clusterIPs", "ipFamilies", "ipFamilyPolicy", "selector")}
    return state


def check_context():
    need(command(["helm", "version", "--template", "{{.Version}}"]).decode() == "v4.3.2", "Require reviewed Helm 4.3.2")
    need(os.environ.get("HELM_DRIVER", "secret") == "secret" and not any(os.environ.get(k) for k in
         ("HELM_KUBECONTEXT", "HELM_KUBEAPISERVER", "HELM_KUBEASUSER", "HELM_KUBEASGROUPS", "HELM_KUBETOKEN")),
         "Ambiguous Helm cluster overrides")
    server = json.loads(kube("get", "--raw=/version"))["gitVersion"]
    need(server == "v1.36.1", "Kubernetes version differs from the rehearsed version")
    uid = get_object("v1:Namespace:-:kube-system")["metadata"]["uid"]
    need(bool(uid) and uid == os.environ.get("SCHEDULED_CLUSTER_UID"), "This Kubernetes cluster is not the independently configured target")
    return {"kubernetes": server, "clusterUID": uid}


def helm_record(ns, name, revision, status):
    ident = f"v1:Secret:{ns}:sh.helm.release.v1.{name}.v{revision}"
    secret = get_object(ident)
    labels = secret["metadata"]["labels"]
    need(secret["type"] == "helm.sh/release.v1" and all(labels[k] == v for k, v in {
        "owner": "helm", "name": name, "status": status, "version": str(revision)}.items()), "Helm ledger labels disagree")
    raw = b64decode(b64decode(secret["data"]["release"], validate=True), validate=True)
    if raw.startswith(b"\x1f\x8b"):
        decoder = zlib.decompressobj(31)
        raw = decoder.decompress(raw, LIMIT + 1)
        need(decoder.eof and not decoder.unused_data, "Invalid compressed Helm record")
    need(len(raw) <= LIMIT, "Oversized Helm record")
    record = json.loads(raw)
    need((record["name"], record["namespace"], record["version"], record["info"]["status"]) ==
         (name, ns, revision, status), "Helm payload identity differs")
    return {"revision": revision, "status": status, "uid": secret["metadata"]["uid"], "sha256": sha(raw)}, record


def apply_method(release):
    # Helm 4 --server-side=auto treats an empty pre-Helm-4 record as CSA.
    method = release.get("apply_method", "")
    need(method in ("", "csa", "ssa"), "Unknown Helm release apply method")
    return method or "csa"


def history(key):
    ns, name, _ = RELEASES[key]
    rows = json.loads(command(["helm", "history", name, "-n", ns, "--max", "256", "-o", "json"]))
    revisions = {row["revision"]: row["status"] for row in rows}
    need(revisions and len(revisions) == len(rows), "Missing or ambiguous release history")
    latest = max(revisions)
    need(revisions[latest] == "deployed" or (revisions[latest] == "failed" and not key.endswith("-jaeger")),
         "Release is missing, pending, or requires operator recovery")
    records, current, successful = [], None, None
    for revision in range(latest, 0, -1):
        status = revisions.get(revision)
        need(status in ("failed", "deployed", "superseded"), "Missing or in-flight intervening Helm history")
        digest, data = helm_record(ns, name, revision, status)
        records.append(digest)
        if current is None:
            current = data
        if status in ("deployed", "superseded"):
            successful = data
            break
    need(successful is not None, "No verified successful rollback target")
    return records, current, successful


def helm_transaction(key, chart, values, *, expected=None, dry=True, atomic=False):
    ns, name, timeout = RELEASES[key]
    need(type(atomic) is bool and (not atomic or key.endswith("-jaeger")), "Unsupported automatic rollback")
    with tempfile.TemporaryDirectory(prefix="jaeger-script-release-") as folder:
        path = Path(folder)
        (path / "chart.tgz").write_bytes(chart)
        (path / "values.json").write_bytes(values)
        env = dict(os.environ, HELM_DRIVER="secret", HELM_PLUGINS=str(Path(__file__).parent / "helm-plugins"))
        args = ["helm", "upgrade", name, str(path / "chart.tgz"), "--namespace", ns, "--values", str(path / "values.json"),
                "--install=false", "--create-namespace=false", "--reset-values", "--skip-crds", "--server-side=auto", "--wait=legacy",
                "--history-max=10", "--timeout", str(timeout) + "s", "--rollback-on-failure=" + str(atomic).lower(), "-o", "json"]
        if dry:
            args.append("--dry-run=server")
        if expected is not None:
            env["JAEGER_APPROVED_MANIFEST"] = str(path / "expected.yaml")
            (path / "expected.yaml").write_bytes(b"\n---\n".join(encode(o) for o in expected))
            args.extend(["--post-renderer", "verify-frozen-manifests"])
        release = json.loads(command(args, env=env, timeout=timeout * (2 if atomic else 1) + 90))
        need(release["name"] == name and release["namespace"] == ns and release["chart"]["metadata"]["name"] == CHARTS[key],
             "Unexpected Helm release or chart")
        return release


def inventory(key, objects, supplements):
    ns = RELEASES[key][0]
    approved = json.loads((ROOT / "docs/all-five-chart-access.json").read_bytes())["records"]
    def allowed(ident, supplemental):
        api, kind, namespace, name = ident.split(":")
        plural = {"Ingress": "ingresses", "Prometheus": "prometheuses"}.get(kind, kind.lower() + "s")
        scope = "cluster" if namespace == "-" else namespace
        group = api.partition("/")[0] if "/" in api else ""
        return any(row["component"] == component(key) and row["apiGroup"] == group
                   and row["resource"] == plural and row["scope"] == scope and row["name"] == name
                   and row["release"] == ("supplement" if supplemental else RELEASES[key][1]) for row in approved)
    ordinary = index([obj for obj in objects if HELM_HOOK not in (obj["metadata"].get("annotations") or {})], ns)
    for obj in objects:
        # Helm test hooks are stored but not run by a scheduled upgrade.
        hook = (obj["metadata"].get("annotations") or {}).get(HELM_HOOK, "")
        if hook and set(hook.split(",")) <= {"test", "test-success"}:
            continue
        need(allowed(identity(obj, ns), False), "Chart object outside reviewed access inventory")
    for ident, obj in index(supplements, ns).items():
        need(allowed(ident, True) and ident not in ordinary, "Supplement outside reviewed access inventory")
        ordinary[ident] = obj
    return ordinary


def checkpoint():
    obj = get_object("v1:ConfigMap:default:" + deploy.STATE)
    return obj["metadata"]["uid"], {k: obj.get("data", {}).get(k) for k in COMPONENTS}


def bindings():
    def get(kind, name):
        names = {"pod": "Pod", "pvc": "PersistentVolumeClaim"}
        return get_object(f"v1:{names[kind]}:opensearch:{name}")
    return list(deploy.opensearch_data_binding(get))


def storage_health(version, dashboards_version):
    if verify_trace.request("http://opensearch-cluster-single.opensearch.svc:9200/")["version"]["number"] != version:
        raise AdapterError("OpenSearch version differs")
    if verify_trace.request("https://opensearch.demo.jaegertracing.io/api/status")["version"]["number"] != dashboards_version:
        raise AdapterError("Dashboards version differs")
    verify_trace.check_trace("http://jaeger-collector.jaeger.svc:4318", "http://jaeger-query.jaeger.svc:16686")


def patch_state(name, fingerprint, expected, assert_locked):
    obj = get_object("v1:ConfigMap:default:" + deploy.STATE)
    uid, data = expected
    need(obj["metadata"]["uid"] == uid and all(obj.get("data", {}).get(k) == data[k] for k in COMPONENTS),
         "Deployment checkpoint drifted")
    change = ({"op": "add", "path": "/data/" + name, "value": fingerprint} if "data" in obj
              else {"op": "add", "path": "/data", "value": {name: fingerprint}})
    ops = tests(obj) + [change]
    assert_locked()
    kube("-n", "default", "patch", "configmap", deploy.STATE, "--type=json", "--patch-file=/dev/stdin", body=encode(ops))


def tests(obj):
    return [{"op": "test", "path": "/metadata/" + k, "value": obj["metadata"][k]} for k in ("uid", "resourceVersion")]


def escape(key):
    return key.replace("~", "~0").replace("/", "~1")


def edits(prior, desired, live, path=""):
    # Reconcile only fields managed by the previous declarative input. Kubernetes
    # defaults and externally managed Service/Ingress fields remain untouched.
    changes = []
    for k in prior.keys() - desired.keys():
        if k in live:
            changes.append({"op": "remove", "path": path + "/" + escape(k)})
    for k, value in desired.items():
        p = path + "/" + escape(k)
        if isinstance(value, dict) and isinstance(live.get(k), dict):
            changes.extend(edits(prior.get(k, {}) if isinstance(prior.get(k), dict) else {}, value, live[k], p))
        elif k not in live or not subset(value, live[k]):
            changes.append({"op": "add", "path": p, "value": value})
    return changes


LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"
RESTARTED = "kubectl.kubernetes.io/restartedAt"


def supplemental_changes(obj, live):
    ns = obj["metadata"]["namespace"]
    ident = identity(obj, ns)
    stored = (live["metadata"].get("annotations") or {}).get(LAST_APPLIED)
    need(stored, "Supplement lacks its previous declarative configuration")
    prior = json.loads(stored)
    need(identity(prior, ns) == ident and obj["kind"] in {"Deployment", "Service", "Ingress", "ConfigMap"}
         and set(obj["metadata"]) <= {"name", "namespace", "labels", "annotations"} and "status" not in obj,
         "Unexpected supplemental object or ownership metadata")

    def declarative(value):
        clean = json.loads(encode(value))
        # Earlier kubectl --dry-run=client versions emit null creationTimestamp.
        # It is server-owned and must never appear in a patch.
        meta = clean["metadata"]
        for field in ("creationTimestamp", "generation", "managedFields", "uid", "resourceVersion"):
            meta.pop(field, None)
        meta["namespace"] = ns
        annotations = meta.get("annotations") or {}
        annotations.pop(LAST_APPLIED, None)
        if not annotations:
            meta.pop("annotations", None)
        if clean["kind"] == "Deployment" and meta["name"] == "trace-generator":
            podmeta = clean["spec"]["template"]["metadata"]
            (podmeta.get("annotations") or {}).pop(RESTARTED, None)
            if not podmeta.get("annotations"):
                podmeta.pop("annotations", None)
        return clean

    old, want = declarative(prior), declarative(obj)
    desired = json.loads(encode(want))
    # A slot-specific rollout restart changes only the pod template. Do not
    # manufacture or rewrite an arbitrary last-applied annotation every day.
    if old != want:
        desired["metadata"].setdefault("annotations", {})[LAST_APPLIED] = encode(want).decode()
    if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "trace-generator":
        stamp = obj["spec"]["template"]["metadata"]["annotations"][RESTARTED]
        desired["spec"]["template"]["metadata"].setdefault("annotations", {})[RESTARTED] = stamp
    if obj["kind"] == "Service":
        for field in ("clusterIP", "clusterIPs", "ipFamilies", "ipFamilyPolicy", "selector"):
            need(field not in desired["spec"] or desired["spec"][field] == live["spec"].get(field),
                 "Supplemental Service binding change requires operator review")
    return edits(old, desired, live)


def patch_supplemental(obj, baseline, assert_locked):
    ns = obj["metadata"]["namespace"]
    ident = identity(obj, ns)
    live = get_object(ident)
    need(projection(live) == baseline, "Supplemental object drifted")
    changes = supplemental_changes(obj, live)
    if changes:
        api, kind, _, name = ident.split(":")
        resource = kind.lower() + ("." + api.split("/")[0] if "/" in api else "")
        assert_locked()
        kube("-n", ns, "patch", resource, name, "--type=json", "--patch-file=/dev/stdin", body=encode(tests(live) + changes))
    need(subset(obj, get_object(ident)), "Supplement repair did not restore the frozen desired fields")


def monitoring_guards(objects):
    import bz2
    cfg = [o for o in objects if o["kind"] == "ConfigMap" and "crds.bz2" in o.get("binaryData", {})]
    need(len(cfg) == 1 and cfg[0]["metadata"]["name"] == "prometheus-crds-upgrade", "Monitoring CRD hook archive missing")
    zipped = b64decode(cfg[0]["binaryData"]["crds.bz2"], validate=True)
    decoder = bz2.BZ2Decompressor()
    raw = decoder.decompress(zipped, max_length=LIMIT)
    need(decoder.eof and not decoder.unused_data, "Invalid CRD hook archive")
    definitions = index(docs(raw), "default")
    need(len(definitions) == 10, "Unexpected CRD hook inventory")
    protected = {}
    for ident, crd in definitions.items():
        need(crd["kind"] == "CustomResourceDefinition" and crd["spec"]["group"] == "monitoring.coreos.com", "Unexpected CRD hook target")
        live = get_object(ident)
        expect = {v["name"]: v["schema"] for v in crd["spec"]["versions"]}
        need(expect == {v["name"]: v["schema"] for v in live["spec"]["versions"]} and
             any(c["type"] == "Established" and c["status"] == "True" for c in live["status"]["conditions"]),
             "Live CRD schema/establishment differs from the monitoring hook")
        protected[ident] = projection(live)
    cert = "v1:Secret:default:prometheus-kube-prometheus-admission"
    secret = get_object(cert)
    ca = secret["data"]["ca"]
    need(ca, "Monitoring webhook certificate authority missing")
    protected[cert] = projection(secret)
    for item in objects:
        if item["kind"] in ("ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"):
            live = get_object(identity(item, "default"))
            need(live["webhooks"] and all(w["clientConfig"].get("caBundle") == ca for w in live["webhooks"]),
                 "Admission webhook does not trust the reviewed certificate")
    return protected


def check_live_rbac(desired, live):
    for field in ("rules", "aggregationRule", "subjects", "roleRef"):
        wanted, actual = desired.get(field), live.get(field)
        if field == "rules" and wanted == [] and actual is None:
            # The Kubernetes RBAC API persists an empty rule set as null.
            continue
        need(wanted == actual, "Live RBAC differs from the protected baseline")


def baseline(key, current, rollback, candidate, extras, images):
    ns = RELEASES[key][0]
    validate_change(key, release_docs(current), candidate, images)
    validate_change(key, release_docs(rollback), candidate, images)
    tracked = inventory(key, candidate, extras)
    extra_ids = set(index(extras, ns))
    result = {}
    for ident, desired in tracked.items():
        live = get_object(ident)
        if ident not in extra_ids:
            meta = live["metadata"]
            labels, annotations = meta.get("labels") or {}, meta.get("annotations") or {}
            need(annotations.get("meta.helm.sh/release-name") == RELEASES[key][1]
                 and annotations.get("meta.helm.sh/release-namespace") == ns
                 and labels.get("app.kubernetes.io/managed-by") == "Helm", "Existing chart resource has foreign ownership")
        if desired["kind"] in {"Role", "ClusterRole", "RoleBinding", "ClusterRoleBinding"}:
            check_live_rbac(desired, live)
        if ident in extra_ids:
            supplemental_changes(desired, live)  # Reject unavailable management history before any Helm write.
        if desired["kind"] == "Secret":
            need(desired.get("data", {}) == live.get("data", {}) and not desired.get("stringData"),
                 "Chart-owned Secret payload differs from the protected baseline")
        if desired["kind"] in {"ValidatingWebhookConfiguration", "MutatingWebhookConfiguration"}:
            def without_ca(webhooks):
                values = json.loads(encode(webhooks))
                for hook in values:
                    hook["clientConfig"].pop("caBundle", None)
                return values
            # API defaulting fills some fields. Reject edits to chart-defined
            # policy fields and unknown webhook identities.
            rendered_hooks, live_hooks = without_ca(desired["webhooks"]), without_ca(live["webhooks"])
            need(len(rendered_hooks) == len(live_hooks) and all(subset(a, b) for a, b in zip(rendered_hooks, live_hooks)),
                 "Live admission configuration differs from the protected baseline")
        result[ident] = projection(live)
    if key == "oci-monitoring":
        result.update(monitoring_guards(candidate))
    return result


def subset(expected, actual):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(k in actual and subset(v, actual[k]) for k, v in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(expected) == len(actual) and all(subset(a, b) for a, b in zip(expected, actual))
    return type(expected) is type(actual) and expected == actual


def prepare(slot, configuration_sha):
    from scheduled_inputs import acquire
    with acquire(configuration_sha) as (root, charts, images):
        return prepare_inputs(slot, configuration_sha, root, charts, images)


def prepare_inputs(slot, configuration_sha, root, charts, images):
    images_valid(images)
    need(HEX40.fullmatch(configuration_sha), "Invalid trusted configuration commit")
    ctx = check_context()
    uid, previous = checkpoint()
    desired = {name: deploy.fingerprint(name, root) for name in COMPONENTS}
    chosen = set(deploy.selected_components("schedule", "all", "all", "upgrade", previous, desired))
    plan = {"format": "scripts-v1", "slot": slot, "configurationSHA": configuration_sha, "context": ctx,
            "previous": previous, "checkpointUID": uid, "fingerprints": desired, "images": images,
            "selected": [k for k in COMPONENTS if k in chosen], "binding": bindings(), "releases": {}, "priorTracking": {}}
    files = {}
    for k in COMPONENTS:
        docs_for = supplemental_for(k, root)
        if k.endswith("-jaeger"):
            generator = next(o for o in docs_for if o["kind"] == "Deployment" and o["metadata"]["name"] == "trace-generator")
            generator["spec"]["template"]["metadata"].setdefault("annotations", {})["kubectl.kubernetes.io/restartedAt"] = slot
        files["supplemental/" + k + ".json"] = encode(docs_for)
    for k, (ns, name, _) in RELEASES.items():
        records, current, rollback = history(k)
        if k.endswith("-jaeger"):
            plan["priorTracking"][k] = json.loads(current["config"]["uiconfig"])["tracking"]
        chart = charts[ns + "/" + name]
        values = encode(values_for(k, root, images))
        atomic, prior_images = rollback_policy(k, rollback)
        prepared = helm_transaction(k, chart, values, atomic=atomic)
        method = apply_method(current)
        need(apply_method(prepared) == method, "Helm preview changed the existing release apply method")
        candidate = release_docs(prepared)
        extras = json.loads(files["supplemental/" + component(k) + ".json"]) if k != "opensearch-dashboards" else []
        states = baseline(k, current, rollback, candidate, extras, images)
        need(records == history(k)[0], "Release history changed during planning")
        plan["releases"][k] = {"records": records, "states": states, "chartVersion": prepared["chart"]["metadata"]["version"],
                               "appVersion": prepared["chart"]["metadata"].get("appVersion", ""), "chartSHA256": sha(chart),
                               "rollbackRenderSHA256": sha(encode(index(release_docs(rollback), ns))),
                               "atomicRollback": atomic, "priorSnapshotPods": prior_images, "applyMethod": method,
                               "imageReferences": sorted(image_references(candidate + extras))}
        files.update({"charts/" + k + ".tgz": chart, "values/" + k + ".json": values, "renders/" + k + ".json": encode(candidate)})
    need((uid, previous) == checkpoint() and plan["binding"] == bindings(), "Checkpoint or storage changed during preparation")
    files["plan.json"] = encode(plan)
    need(set(files) == MEMBERS, "Incomplete scheduled bundle")
    return files


def validate_plan(files, slot, configuration_sha):
    need(set(files) == MEMBERS and all(isinstance(b, bytes) and 0 < len(b) <= LIMIT for b in files.values()), "Invalid bundle members")
    plan = json.loads(files["plan.json"])
    need((plan["format"], plan["slot"], plan["configurationSHA"]) == ("scripts-v1", slot, configuration_sha)
         and HEX40.fullmatch(configuration_sha), "Bundle belongs to another configuration or slot")
    images_valid(plan["images"])
    need(set(plan["fingerprints"]) == set(plan["previous"]) == set(COMPONENTS) and set(plan["releases"]) == set(RELEASES)
         and all(HEX64.fullmatch(v) for v in plan["fingerprints"].values()), "Incomplete component plan")
    chosen = set(deploy.selected_components("schedule", "all", "all", "upgrade", plan["previous"], plan["fingerprints"]))
    need(plan["selected"] == [k for k in COMPONENTS if k in chosen], "Bundle selection differs from daily policy")
    for key, row in plan["releases"].items():
        need(sha(files["charts/" + key + ".tgz"]) == row["chartSHA256"], "Chart package differs from plan")
    return plan


def material(files, key):
    return (files["charts/" + key + ".tgz"], files["values/" + key + ".json"], json.loads(files["renders/" + key + ".json"]),
            json.loads(files["supplemental/" + component(key) + ".json"]) if key != "opensearch-dashboards" else [])


def check_jaeger_patch_access(namespace, candidate):
    # Helm may patch even semantically unchanged Services and CronJobs under
    # CSA, or every object under SSA. Dry-run rendering cannot check RBAC.
    resources = {("v1", "ConfigMap"): "configmaps", ("v1", "ServiceAccount"): "serviceaccounts",
                 ("v1", "Service"): "services", ("apps/v1", "Deployment"): "deployments",
                 ("batch/v1", "CronJob"): "cronjobs"}
    for ident in sorted(index(candidate, namespace)):
        api, kind, ns, name = ident.split(":")
        resource = resources.get((api, kind))
        need(resource and namespace == ns, "Unreviewed ordinary Jaeger chart write")
        attrs = {"namespace": namespace, "verb": "patch", "group": api.rpartition("/")[0],
                 "resource": resource, "name": name}
        review = {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
                  "spec": {"resourceAttributes": attrs}}
        status = json.loads(kube("create", "--raw=/apis/authorization.k8s.io/v1/selfsubjectaccessreviews",
                                 "-f", "-", body=encode(review)))["status"]
        need(status["allowed"] is True and not status.get("denied") and not status.get("evaluationError"),
             "Executor lacks reviewed PATCH access to a named Jaeger chart object")


def preconditions(files, key, plan, *, dry=True):
    row = plan["releases"][key]
    chart, values, candidate, extras = material(files, key)
    records, current, rollback = history(key)
    need(records == row["records"] and apply_method(current) == row["applyMethod"], "Helm revision, apply method or stored rollback data drifted")
    need(rollback_policy(key, rollback) == (row["atomicRollback"], row["priorSnapshotPods"]),
         "Rollback image reference, digest or eligibility changed")
    need(baseline(key, current, rollback, candidate, extras, plan["images"]) == row["states"],
         "Live object content, identity or binding drifted")
    if dry:
        preview = helm_transaction(key, chart, values, expected=candidate, atomic=row["atomicRollback"])
        ns = RELEASES[key][0]
        need(index(release_docs(preview), ns) == index(candidate, ns) and
             preview["chart"]["metadata"]["version"] == row["chartVersion"] and apply_method(preview) == row["applyMethod"],
             "Server-side render or apply method differs from the signed plan")


def verify_upgrade(key, row, candidate):
    ns, _, _ = RELEASES[key]
    records, current, _ = history(key)
    need(records[0]["status"] == "deployed" and records[0]["revision"] == row["records"][0]["revision"] + 1
         and apply_method(current) == row["applyMethod"] and index(release_docs(current), ns) == index(candidate, ns),
         "Stored upgrade differs from the frozen transaction")
    if key == "oci-monitoring":
        monitoring_guards(candidate)
    for ident, before in row["states"].items():
        obj = get_object(ident)
        after = projection(obj)
        need(before["uid"] == after["uid"] and before.get("binding") == after.get("binding"), "Protected object or Service binding changed")
        if obj["kind"] in PROTECTED:
            need(before["state"] == after["state"], "Protected security or schema object changed during upgrade")


def snapshot_pods(key):
    ns = RELEASES[key][0]
    evidence = {}
    for workload, image in (("jaeger", "jaeger"), ("jaeger-hotrod", "hotrod")):
        deployment = get_object(f"apps/v1:Deployment:{ns}:{workload}")
        meta, spec, status = deployment["metadata"], deployment["spec"], deployment["status"]
        count = spec.get("replicas", 1)
        need(count > 0 and status["observedGeneration"] == meta["generation"] and
             all(status.get(k) == count for k in ("replicas", "readyReplicas", "updatedReplicas", "availableReplicas")),
             "Snapshot Deployment is still reconciling")
        labels = spec["selector"].get("matchLabels", {})
        need(labels and not spec["selector"].get("matchExpressions"), "Unsupported snapshot Deployment selector")
        selector = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        pods = json.loads(kube("-n", ns, "get", "pods", "-l", selector, "-o", "json"))["items"]
        active = [p for p in pods if not p["metadata"].get("deletionTimestamp")]
        need(len(active) == count, "Unexpected snapshot Pod count")
        def controller(obj, kind):
            refs = [r for r in obj["metadata"].get("ownerReferences", []) if r.get("controller") is True]
            need(len(refs) == 1 and refs[0]["kind"] == kind and refs[0]["apiVersion"] == "apps/v1", "Unexpected Pod owner")
            return refs[0]
        spec_refs = [c["image"].removeprefix("docker.io/") for c in spec["template"]["spec"]["containers"] if c["name"] == workload]
        need(len(spec_refs) == 1, "Snapshot Deployment has an unexpected container")
        digests = set()
        for pod in active:
            parent = controller(pod, "ReplicaSet")
            rs = get_object("apps/v1:ReplicaSet:" + ns + ":" + parent["name"])
            owner = controller(rs, "Deployment")
            need(rs["metadata"]["uid"] == parent["uid"] and owner["name"] == workload and owner["uid"] == meta["uid"],
                 "Pod does not belong to the intended Deployment")
            containers = [c["image"].removeprefix("docker.io/") for c in pod["spec"]["containers"] if c["name"] == workload]
            actual = [c for c in pod["status"].get("containerStatuses", []) if c["name"] == workload]
            need(containers == spec_refs and pod["status"]["phase"] == "Running" and
                 any(c["type"] == "Ready" and c["status"] == "True" for c in pod["status"].get("conditions", [])) and
                 len(actual) == 1 and actual[0]["ready"] is True, "Snapshot Pod is not ready on its declared image")
            observed = actual[0]["imageID"].split("@")[-1].removeprefix("docker://")
            need(re.fullmatch(r"sha256:[a-f0-9]{64}", observed), "Snapshot Pod lacks an immutable runtime digest")
            digests.add(observed)
        evidence[image] = {"reference": spec_refs[0], "runtimeDigests": sorted(digests)}
    return evidence


def ready_images(key, sources):
    for role, observed in snapshot_pods(key).items():
        src = sources[role]
        need(observed["reference"] == src["reference"].removeprefix("docker.io/") and
             set(observed["runtimeDigests"]) <= {src["digest"], src["platformManifest"]},
             "Running snapshot Pod image differs from the verified registry digest")


def rollback_policy(key, release):
    if not key.endswith("-jaeger"):
        return False, {}
    workloads = index(release_docs(release), RELEASES[key][0])
    observed = snapshot_pods(key)
    pinned = True
    for role, name in (("jaeger", "jaeger"), ("hotrod", "jaeger-hotrod")):
        obj = workloads[f"apps/v1:Deployment:{RELEASES[key][0]}:{name}"]
        refs = [c["image"].removeprefix("docker.io/") for c in obj["spec"]["template"]["spec"]["containers"] if c["name"] == name]
        need(refs == [observed[role]["reference"]], "Ready snapshot Pods differ from the stored successful rollback manifest")
        pinned = pinned and bool(re.search(r"@sha256:[a-f0-9]{64}$", refs[0]))
    return pinned, observed


def monitoring_workload_current(obj):
    spec, status = obj["spec"], obj.get("status", {})
    count = spec.get("replicas", 1) * spec.get("shards", 1)
    generation = obj["metadata"]["generation"]
    return (count > 0 and not spec.get("paused") and not status.get("paused")
            and all(status.get(field) == count for field in ("replicas", "availableReplicas", "updatedReplicas"))
            and status.get("unavailableReplicas", 0) == 0
            and {"Available", "Reconciled"} <= {
                condition["type"] for condition in status.get("conditions", [])
                if condition.get("status") == "True" and condition.get("observedGeneration") == generation})


def check_monitoring_workloads(*, wait):
    # The operator's two workload CRs have a real status subresource. Legacy
    # Helm checks native controllers with named reads; check these explicitly.
    deadline = time.monotonic() + (300 if wait else 0)
    while True:
        if all(monitoring_workload_current(get_object(
                f"monitoring.coreos.com/v1:{kind}:default:prometheus-kube-prometheus-{kind.lower()}"))
               for kind in ("Alertmanager", "Prometheus")):
            return
        need(time.monotonic() < deadline, "Monitoring workloads are not available and reconciled at their current generation")
        time.sleep(2)


def rollout(key):
    ns = RELEASES[key][0]
    checks = {"oci-monitoring": ["deployment/prometheus-grafana"], "oci-jaeger": ["deployment/jaeger", "deployment/jaeger-hotrod", "deployment/trace-generator"],
              "opensearch": ["statefulset/opensearch-cluster-single"], "opensearch-dashboards": ["deployment/opensearch-dashboards"],
              "otel-jaeger": ["deployment/jaeger", "deployment/jaeger-hotrod", "deployment/trace-generator"],
              "otel-demo": ["daemonset/otel-collector-agent", "deployment/frontend", "deployment/load-generator"]}
    for name in checks[key]:
        kube("-n", ns, "rollout", "status", name, "--timeout=10m", timeout=610)
    if key == "oci-monitoring":
        check_monitoring_workloads(wait=True)


def apply(files, *, slot, configuration_sha, assert_locked):
    assert_locked()
    plan = validate_plan(files, slot, configuration_sha)
    need(check_context() == plan["context"] and tuple(checkpoint()) == (plan["checkpointUID"], plan["previous"])
         and bindings() == plan["binding"], "Cluster, checkpoint or PVC changed before execution")
    for key in RELEASES:
        assert_locked()
        preconditions(files, key, plan, dry=component(key) in plan["selected"])
        if key.endswith("-jaeger") and key in plan["selected"]:
            check_jaeger_patch_access(RELEASES[key][0], json.loads(files["renders/" + key + ".json"]))
    if "oci-monitoring" in plan["selected"]:
        check_monitoring_workloads(wait=False)
    tracking = dict(plan["priorTracking"])
    for stack in ("oci", "otel"):
        stack_health.check(stack, tracking=tracking[stack + "-jaeger"])
    previous = dict(plan["previous"])
    for key in RELEASES:
        part = component(key)
        if part not in plan["selected"]:
            continue
        chart, values, candidate, extras = material(files, key)
        row = plan["releases"][key]
        assert_locked()
        need(checkpoint() == (plan["checkpointUID"], previous) and bindings() == plan["binding"],
             "Deployment checkpoint or PVC changed before write")
        preconditions(files, key, plan, dry=False)
        assert_locked()
        try:
            helm_transaction(key, chart, values, expected=candidate, dry=False, atomic=row["atomicRollback"])
        except AdapterError:
            if key.endswith("-jaeger") and not row["atomicRollback"]:
                raise AdapterError("Initial digest promotion failed; partial changes may remain. Automatic tag-based rollback was disabled; operator recovery required") from None
            if row["atomicRollback"]:
                records, result, _ = history(key)
                need(records[0]["status"] == "deployed" and records[0]["revision"] == row["records"][0]["revision"] + 2 and
                     apply_method(result) == row["applyMethod"] and
                     sha(encode(index(release_docs(result), RELEASES[key][0]))) == row["rollbackRenderSHA256"] and
                     snapshot_pods(key) == row["priorSnapshotPods"],
                     "Upgrade failed; automatic rollback remains unverified")
                raise AdapterError("Upgrade failed; Helm restored the prior render; operator recovery required") from None
            raise
        for obj in extras:
            assert_locked()
            patch_supplemental(obj, row["states"][identity(obj, RELEASES[key][0])], assert_locked)
        rollout(key)
        verify_upgrade(key, row, candidate)
        if key.endswith("-jaeger"):
            ready_images(key, plan["images"])
        need(bindings() == plan["binding"], "OpenSearch PVC or PV binding changed")
        if key == "opensearch":
            continue  # Both storage releases share a checkpoint.
        if key == "opensearch-dashboards":
            storage_health(plan["releases"]["opensearch"]["appVersion"], row["appVersion"])
        if key.endswith("-jaeger"):
            tracking[key] = json.loads(json.loads(values)["uiconfig"])["tracking"]
        stack = "oci" if key.startswith("oci-") else "otel"
        stack_health.check(stack, tracking=tracking[stack + "-jaeger"])
        assert_locked()
        patch_state(part, plan["fingerprints"][part], (plan["checkpointUID"], previous), assert_locked)
        previous[part] = plan["fingerprints"][part]
    need(checkpoint() == (plan["checkpointUID"], previous), "Final fingerprints differ from the verified selection")
    assert_locked()
    return True
