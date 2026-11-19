#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Acquire Helmfile adoption observations with named, read-only Kubernetes API requests."""

import argparse
import base64
import contextlib
import http.client
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlencode
import zlib

from adoption_bundle import RELEASES, canonical, digest, read_json, require
from promotion_images import REPOS, STACKS, registry_image
from stack_health import check as hosted_check


METADATA = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
DNS = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*")
KIND = re.compile(r"[A-Za-z][A-Za-z0-9]*")
LIMIT = 32 * 1024 * 1024
STATUSES = {"unknown", "deployed", "uninstalled", "superseded", "failed", "uninstalling",
            "pending-install", "pending-upgrade", "pending-rollback"}
TRANSIENT_METADATA = {"resourceVersion", "managedFields", "generation", "creationTimestamp"}


class Connection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=30)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


@contextlib.contextmanager
def reader(kubeconfig, context, kubectl="kubectl"):
    # The socket and its directory are owner-only. Never place a Secret in a
    # subprocess argument, file, log, exception, or generic kubectl output.
    with tempfile.TemporaryDirectory(prefix="demo-api-") as tmp:
        path = str(Path(tmp) / "k.sock")
        args = [kubectl, "--kubeconfig", str(kubeconfig), "--context", context, "proxy",
                "--unix-socket", path, "--accept-hosts=^localhost$",
                "--reject-methods=^(POST|PUT|PATCH|DELETE|CONNECT|TRACE|HEAD|OPTIONS)$"]
        with subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as proc:
            try:
                for _ in range(150):
                    require(proc.poll() is None, "Kubernetes API proxy exited")
                    if os.path.exists(path):
                        break
                    time.sleep(0.1)
                else:
                    raise ValueError("Kubernetes API proxy did not start")
                def get(url, accept="application/json"):
                    with contextlib.closing(Connection(path)) as conn:
                        conn.request("GET", url, headers={"Accept": accept})
                        response = conn.getresponse()
                        if response.status == 404:
                            return None
                        require(response.status == 200, f"Kubernetes read failed with HTTP {response.status}")
                        data = response.read(LIMIT + 1)
                        require(len(data) <= LIMIT, "Oversized Kubernetes response")
                        return read_json(data)
                yield get
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()


def discover(get):
    core = get("/api")
    grouped = get("/apis")
    require(core["kind"] == "APIVersions" and grouped["kind"] == "APIGroupList", "Incomplete API discovery")
    groups = list(core["versions"])
    groups.extend(v["groupVersion"] for group in grouped["groups"] for v in group["versions"])
    require(groups and len(groups) == len(set(groups)), "Duplicate API group versions")
    routes, versions = {}, set(groups)
    for group in groups:
        require(all(DNS.fullmatch(part) for part in group.split("/")) and group.count("/") <= 1, "Invalid API group version")
        base = ("/apis/" if "/" in group else "/api/") + group
        resources = get(base)
        require(resources and resources["kind"] == "APIResourceList" and resources["groupVersion"] == group, "Incomplete API group")
        for entry in resources["resources"]:
            kind, plural = entry["kind"], entry["name"]
            require(KIND.fullmatch(kind), "Invalid API kind")
            versions.add(group + "/" + kind)  # Helm includes subresource kinds in Capabilities.APIVersions.
            if "/" not in plural and "get" in entry["verbs"]:
                require(DNS.fullmatch(plural) and type(entry["namespaced"]) is bool, "Invalid API resource")
                key = group, kind
                # A GVK can appear through multiple endpoints. Abort if the plan selects an ambiguous one.
                route = base, plural, entry["namespaced"]
                routes[key] = route if routes.get(key, route) == route else None
    version = get("/version")["gitVersion"]
    require(re.fullmatch(r"v\d+\.\d+\.\d+", version), "Unexpected Kubernetes version")
    return routes, {"kubernetesVersion": version, "apiVersions": sorted(versions)}


def item_path(identity, routes):
    version, kind, ns, name = identity.split(":")
    route = routes.get((version, kind))
    require(route and DNS.fullmatch(name) and len(name) <= 253, "Unknown, ambiguous or invalid named object")
    prefix, plural, namespaced = route
    require((ns != "-" and DNS.fullmatch(ns) and len(ns) <= 63) if namespaced else ns == "-", "Object scope differs from discovery")
    return prefix + ("/namespaces/" + ns if namespaced else "") + "/" + plural + "/" + name


def object_state(identity, obj):
    if obj is None:
        return None
    meta = obj["metadata"]
    require(identity.split(":") == [obj["apiVersion"], obj["kind"], meta.get("namespace", "-"), meta["name"]]
            and isinstance(meta["uid"], str) and meta["uid"], "Object identity changed")
    state = {key: value for key, value in obj.items() if key not in {"metadata", "status"}}
    state["metadata"] = {k: v for k, v in meta.items() if k not in TRANSIENT_METADATA}
    return {"uid": meta["uid"], "stateSHA256": digest(canonical(state))}


def pages(get, path, selector, accept, api, kind):
    token, seen, snapshot = "", set(), None
    for _ in range(100):
        params = {"labelSelector": selector, "limit": 100}
        if token:
            params["continue"] = token
        page = get(path + "?" + urlencode(params), accept)
        require(page and page["apiVersion"] == api and page["kind"] == kind, "Unexpected collection representation")
        version = page["metadata"]["resourceVersion"]
        require(isinstance(version, str) and version and snapshot in (None, version), "Inconsistent list snapshot")
        snapshot = version
        items = page["items"]
        # Empty PartialObjectMetadataList can serialize the Go nil slice as null.
        require(isinstance(items, list) or items is None, "Invalid collection items")
        yield from items or []
        token = page["metadata"].get("continue", "")
        if not token:
            return
        require(isinstance(token, str) and len(token) <= 8192 and token not in seen, "Invalid page continuation")
        seen.add(token)
    raise ValueError("Excessive Kubernetes pagination")


def history_identity(ns, name, meta):
    labels = meta["labels"]
    version = labels["version"]
    require(isinstance(version, str) and re.fullmatch(r"[1-9][0-9]{0,9}", version)
            and meta["name"] == f"sh.helm.release.v1.{name}.v{version}" and meta["namespace"] == ns
            and labels["name"] == name and labels["owner"] == "helm" and labels["status"] in STATUSES
            and meta["uid"] and meta["resourceVersion"] and not meta.get("deletionTimestamp"), "Invalid Helm record identity")
    return int(version), (labels["status"], meta["uid"], meta["resourceVersion"])


def ledger(get, ns, name):
    path = f"/api/v1/namespaces/{ns}/secrets"
    revisions = {}
    for item in pages(get, path, f"owner=helm,name={name}", METADATA, "meta.k8s.io/v1", "PartialObjectMetadataList"):
        require(set(item) <= {"apiVersion", "kind", "metadata"}, "Expected metadata-only Secret item")
        rev, identity = history_identity(ns, name, item["metadata"])
        require(rev not in revisions, "Duplicate Helm revision")
        revisions[rev] = identity
    return revisions


def record(get, ns, name, revision, identity):
    obj = get(f"/api/v1/namespaces/{ns}/secrets/sh.helm.release.v1.{name}.v{revision}")
    require(obj and (obj["apiVersion"], obj["kind"], obj["type"]) == ("v1", "Secret", "helm.sh/release.v1")
            and history_identity(ns, name, obj["metadata"]) == (revision, identity), "Missing or changed Helm record")
    raw = base64.b64decode(base64.b64decode(obj["data"]["release"], validate=True), validate=True)
    if raw.startswith(b"\x1f\x8b"):
        decoder = zlib.decompressobj(31)
        raw = decoder.decompress(raw, LIMIT + 1)
        require(decoder.eof and not decoder.unused_data, "Oversized or invalid compressed Helm record")
    require(len(raw) <= LIMIT, "Oversized Helm record")
    payload = read_json(raw)
    require((payload["namespace"], payload["name"], payload["version"], payload["info"]["status"]) ==
            (ns, name, revision, identity[0]) and type(payload["version"]) is int
            and payload["chart"]["metadata"]["name"] and payload["chart"]["metadata"]["version"],
            "Helm record payload disagrees with its identity or status")
    # Hash all decoded release bytes, including hooks, manifests, chart files and values.
    return {"status": identity[0], "revision": revision, "recordSHA256": digest(raw)}


def history(get, release, rollback):
    ns, name = release.split("/")
    revisions = ledger(get, ns, name)
    if not revisions:
        require(name == "demo-resources" and rollback in (None, 0), "Required release history is absent")
        return {"status": "absent", "revision": 0, "recordSHA256": None}, None
    latest = max(revisions)
    require(revisions[latest][0] in {"deployed", "failed"}, "Latest Helm revision requires operator review")
    # Helm atomic upgrade picks the highest deployed/superseded revision.
    # An undiscoverable intermediate record could be a newer rollback target.
    target, current = None, None
    for rev in range(latest, 0, -1):
        require(rev in revisions, "Gap before Helm rollback target")
        item = record(get, ns, name, rev, revisions[rev])
        if rev == latest:
            current = item
        if item["status"] in {"deployed", "superseded"}:
            target = item
            break
        require(item["status"] == "failed", "Unresolved intervening Helm record")
    require(target and rollback in (None, target["revision"]), "Rollback revision differs from Helm selection")
    require(revisions == ledger(get, ns, name), "Helm ledger changed during acquisition")
    return current, target


def one(items, test, description):
    found = [i for i in items if test(i)]
    require(len(found) == 1, "Expected one " + description)
    return found[0]


def owner(meta, kind, name=None, uid=None):
    controller = one(meta["ownerReferences"], lambda ref: ref.get("controller") is True, "controlling owner")
    require(controller["kind"] == kind and controller["apiVersion"] == "apps/v1" and DNS.fullmatch(controller["name"])
            and controller["uid"] and (name is None or controller["name"] == name)
            and (uid is None or controller["uid"] == uid), "Pod owner chain differs from the named Deployment")
    return controller


def live_image(get, resolve_image, ns, component):
    workload = "jaeger-hotrod" if component == "hotrod" else "jaeger"
    base = f"/apis/apps/v1/namespaces/{ns}"
    dep = get(base + "/deployments/" + workload)
    object_state(f"apps/v1:Deployment:{ns}:{workload}", dep)
    require(dep and not dep["metadata"].get("deletionTimestamp"), "Required Deployment is missing or terminating")
    ds = dep["status"]
    replicas = dep["spec"].get("replicas", 1)
    require(replicas > 0 and ds.get("observedGeneration") == dep["metadata"]["generation"] and
            all(ds.get(k) == replicas for k in ("replicas", "readyReplicas", "updatedReplicas", "availableReplicas"))
            and not ds.get("unavailableReplicas"), "Deployment is still reconciling")
    def image_ref(spec):
        return one(spec["containers"], lambda c: c["name"] == workload, workload + " container")["image"]
    reference = image_ref(dep["spec"]["template"]["spec"])
    repo = REPOS[component]
    match = re.fullmatch(r"(?:docker\.io/)?" + re.escape(repo) +
                         r":([\w][\w.-]{0,127})(?:@(sha256:[a-f0-9]{64}))?", reference, re.ASCII)
    require(match, "Unexpected live snapshot repository or reference")
    src = resolve_image(repo, match[1], reference_digest=match[2])
    require(match[2] in (None, src["digest"]), "Live reference differs from immutable registry manifest")
    selector = dep["spec"]["selector"]
    labels = selector.get("matchLabels", {})
    require(labels and not selector.get("matchExpressions"), "Unsupported hosted Deployment selector")
    selector_text = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
    revisions, uids = {}, set()
    for pod in pages(get, f"/api/v1/namespaces/{ns}/pods", selector_text, "application/json", "v1", "PodList"):
        meta = pod["metadata"]
        if meta.get("deletionTimestamp"):
            continue
        require((pod["apiVersion"], pod["kind"], meta["namespace"]) == ("v1", "Pod", ns)
                and meta["uid"] not in uids and all(meta["labels"].get(k) == v for k, v in labels.items()),
                "Unexpected or duplicate selected Pod")
        uids.add(meta["uid"])
        control = owner(meta, "ReplicaSet")
        rs_name = control["name"]
        if rs_name not in revisions:
            rs = get(base + "/replicasets/" + rs_name)
            require(rs and rs["metadata"]["uid"] == control["uid"] and not rs["metadata"].get("deletionTimestamp")
                    and (rs["apiVersion"], rs["kind"], rs["metadata"]["namespace"], rs["metadata"]["name"]) ==
                    ("apps/v1", "ReplicaSet", ns, rs_name), "Missing or changed ReplicaSet")
            owner(rs["metadata"], "Deployment", workload, dep["metadata"]["uid"])
            revisions[rs_name] = rs["metadata"]["uid"]
        require(revisions[rs_name] == control["uid"] and pod["status"]["phase"] == "Running"
                and image_ref(pod["spec"]) == reference and
                any(c["type"] == "Ready" and c["status"] == "True" for c in pod["status"]["conditions"]), "Pod is not ready on the intended image")
        actual = one(pod["status"]["containerStatuses"], lambda c: c["name"] == workload, "runtime container status")
        observed_digest = actual["imageID"].split("@")[-1].removeprefix("docker://")
        require(actual["ready"] is True and observed_digest in {src["digest"], src["platformManifest"]}, "Running Pod digest differs from the registry")
    require(len(uids) == replicas, "Could not verify all Deployment replicas")
    return {"reference": reference, "sourceCommit": src["sourceCommit"], "digest": src["digest"]}


def functional_health(stacks):
    for stack in stacks:
        hosted_check(stack)
    return True


def observe(scope, get, check_health=functional_health, resolve_image=registry_image):
    """Pass only scope identities, optionally omitting cluster/rollback expectations during planning."""
    require(set(scope) <= {"cluster", "releases", "objects", "imageStacks", "rollbackRevisions"}, "Unexpected observation inputs")
    releases, objects, image_stacks = (scope[k] for k in ("releases", "objects", "imageStacks"))
    require(isinstance(releases, list) and releases and len(set(releases)) == len(releases) and set(releases) <= RELEASES,
            "Unknown or duplicate Helm releases")
    require(isinstance(objects, list) and len(set(objects)) == len(objects) and len(objects) <= 2048, "Invalid observation scope")
    expected = {stack for stack, ns in STACKS.items() if ns + "/jaeger" in releases}
    require(isinstance(image_stacks, list) and len(set(image_stacks)) == len(image_stacks) and set(image_stacks) == expected, "Image scope differs from selected releases")
    rollbacks = scope.get("rollbackRevisions")
    require(rollbacks is None or (set(rollbacks) == set(releases) and
            all(type(n) is int and n >= 0 for n in rollbacks.values())), "Incomplete rollback scope")
    routes, caps = discover(get)
    cluster = get("/api/v1/namespaces/kube-system")
    key = "v1:Namespace:-:kube-system"
    cluster_id = object_state(key, cluster)
    require(cluster_id and scope.get("cluster", cluster_id["uid"]) == cluster_id["uid"], "Cluster identity changed")
    result = {"cluster": cluster_id["uid"], "capabilitiesSHA256": digest(canonical(caps)), "objects": {},
              "releases": {}, "rollbackTargets": {}, "images": {}}
    for identity in objects:
        result["objects"][identity] = object_state(identity, get(item_path(identity, routes)))
    for release in releases:
        result["releases"][release], result["rollbackTargets"][release] = history(get, release, rollbacks and rollbacks[release])
    for stack in image_stacks:
        result["images"][stack] = {name: live_image(get, resolve_image, STACKS[stack], name) for name in REPOS}
    stacks = sorted({"oci" if release.startswith("default/") else "otel" for release in releases})
    require(check_health(stacks) is True, "Fresh functional health check failed")
    result["healthy"] = True
    return result, caps


def live_reader(kubeconfig, context):
    def read_live(scope):
        with reader(kubeconfig, context) as get:
            return observe(scope, get)[0]
    return read_live


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--render-context", type=Path, help="Write fresh Kubernetes capabilities to a new file")
    args = parser.parse_args()
    try:
        scope = read_json(args.scope.read_bytes())
        with reader(args.kubeconfig, args.context) as get:
            result, caps = observe(scope, get)
        if args.render_context:
            with args.render_context.open("xb") as out:
                out.write(canonical(caps))
        print(canonical(result).decode())
    except (ValueError, KeyError, IndexError, TypeError, OSError, http.client.HTTPException) as error:
        # Never echo exception data originating in stored manifests or HTTP payloads.
        parser.exit(1, "Live acquisition failed. No observation was emitted (" + type(error).__name__ + ").\n")


if __name__ == "__main__":
    main()
