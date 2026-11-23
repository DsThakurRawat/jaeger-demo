#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Read object fingerprints and live image references with named, read-only Kubernetes API requests."""

import argparse
import contextlib
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlencode

from promotion_images import REPOS, STACKS, registry_image


RELEASES = {
    "default/jaeger", "default/prometheus", "jaeger/jaeger", "opensearch/opensearch",
    "opensearch/opensearch-dashboards", "otel-demo/otel-demo",
    *(f"{ns}/demo-resources" for ns in ("default", "jaeger", "opensearch", "otel-demo")),
}
METADATA = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
DNS = re.compile(r"[a-z0-9](?:[-a-z0-9]*[a-z0-9])?(?:\.[a-z0-9](?:[-a-z0-9]*[a-z0-9])?)*")
KIND = re.compile(r"[A-Za-z][A-Za-z0-9]*")
LIMIT = 32 * 1024 * 1024
TRANSIENT_METADATA = {"resourceVersion", "managedFields", "generation", "creationTimestamp"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def read_json(data):
    def pairs(items):
        obj = dict(items)
        require(len(obj) == len(items), "Duplicate JSON property")
        return obj
    def invalid_number(_):
        raise ValueError("Non-finite JSON number")
    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_number)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


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
        # The typed PodList already identifies the item type. Its embedded Pods
        # can omit TypeMeta on the wire. Reject any conflicting supplied value.
        require(pod.get("apiVersion", "v1") in ("", "v1") and pod.get("kind", "Pod") in ("", "Pod") and meta["namespace"] == ns
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


def snapshot(get, objects, stacks, resolve_image=registry_image):
    """Fingerprint the named objects and read the live image references of the named stacks."""
    require(len(set(objects)) == len(objects) <= 2048 and len(set(stacks)) == len(stacks)
            and set(stacks) <= set(STACKS) and (objects or stacks), "Invalid or empty read scope")
    result = {"objects": {}, "images": {}}
    if objects:
        routes, _ = discover(get)
        for identity in objects:
            result["objects"][identity] = object_state(identity, get(item_path(identity, routes)))
    for stack in stacks:
        result["images"][stack] = {name: live_image(get, resolve_image, STACKS[stack], name) for name in REPOS}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--object", action="append", default=[], metavar="API_VERSION:KIND:NAMESPACE:NAME",
                        help="Print the UID and state SHA-256 of this object; use - as the namespace of a cluster-scoped object")
    parser.add_argument("--stack", action="append", default=[], choices=sorted(STACKS),
                        help="Print the live image reference, source commit and digest of each component in this stack")
    args = parser.parse_args(argv)
    if not args.object and not args.stack:
        parser.error("name at least one --object or --stack")
    try:
        with reader(args.kubeconfig, args.context) as get:
            result = snapshot(get, args.object, args.stack)
        print(canonical(result).decode())
    except (ValueError, KeyError, IndexError, TypeError, OSError, http.client.HTTPException) as error:
        # Never echo exception data originating in stored objects or HTTP payloads.
        parser.exit(1, "Live read failed. Nothing was printed (" + type(error).__name__ + ").\n")


if __name__ == "__main__":
    main()
