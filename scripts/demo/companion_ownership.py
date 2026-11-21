#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Prepare metadata-only ownership patches for the four namespace-local releases.

The default plan adopts the allowlisted objects. With --de-adopt it reverses the record of
applied adoption patches that apply-ownership-patches.sh wrote. Both only read named objects
and print JSON Patch documents for review.
"""

import argparse
import json
from pathlib import Path
import subprocess

import yaml

from companion_values import ROOT


ALLOWLIST = ROOT / "charts" / "demo-resources" / "allowlist.yaml"
RELEASE = "demo-resources"
API = {"ConfigMap": "v1", "Service": "v1", "Deployment": "apps/v1", "Ingress": "networking.k8s.io/v1"}


def ownership_patch(namespace, kind, name, live):
    meta = live["metadata"]
    if (live["apiVersion"], live["kind"], meta["namespace"], meta["name"]) != (API[kind], kind, namespace, name):
        raise ValueError("Live resource identity does not match the requested resource")
    if not meta.get("uid") or not meta.get("resourceVersion") or meta.get("deletionTimestamp") or meta.get("ownerReferences"):
        raise ValueError(f"{namespace}/{kind}/{name} must have a stable, independently owned identity")

    labels = meta.get("labels", {})
    annotations = meta.get("annotations", {})
    owner = (labels.get("app.kubernetes.io/managed-by"), annotations.get("meta.helm.sh/release-name"),
             annotations.get("meta.helm.sh/release-namespace"))
    if owner not in ((None, None, None), ("Helm", RELEASE, namespace)):
        raise ValueError(f"Conflicting or incomplete Helm ownership for {namespace}/{kind}/{name}")
    if annotations.get("helm.sh/resource-policy", "keep") != "keep":
        raise ValueError(f"Conflicting retention policy for {namespace}/{kind}/{name}")

    updates = []
    for key, desired in (
        ("labels", {"app.kubernetes.io/managed-by": "Helm"}),
        ("annotations", {"meta.helm.sh/release-name": RELEASE, "meta.helm.sh/release-namespace": namespace,
                         "helm.sh/resource-policy": "keep"}),
    ):
        if key not in meta:
            updates.append({"op": "add", "path": "/metadata/" + key, "value": desired})
            continue
        for field, value in desired.items():
            if meta[key].get(field) != value:
                updates.append({"op": "add", "path": "/metadata/" + key + "/" + field.replace("/", "~1"), "value": value})
    if not updates:
        return []
    return [
        {"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
        *updates,
    ]


def read_live(kubeconfig, context, namespace, kind, name):
    cmd = ["kubectl", "--kubeconfig", kubeconfig, "--context", context, "--namespace", namespace,
           "get", kind, name, "--output=json"]
    return json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)


def plan(kubeconfig, context, namespace=None):
    allowlist = yaml.safe_load(ALLOWLIST.read_text())
    result = []
    for ns in ([namespace] if namespace else allowlist):
        for kind, names in allowlist[ns].items():
            for name in names:
                patch = ownership_patch(ns, kind, name, read_live(kubeconfig, context, ns, kind, name))
                result.append({"namespace": ns, "kind": kind, "name": name, "patch": patch})
    return result


def release_patch(namespace, kind, name, live, recorded):
    """Reverse one recorded adoption patch: remove exactly the ownership metadata it added."""
    meta = live["metadata"]
    if (live["apiVersion"], live["kind"], meta["namespace"], meta["name"]) != (API[kind], kind, namespace, name):
        raise ValueError("Live resource identity does not match the requested resource")
    if not meta.get("uid") or not meta.get("resourceVersion") or meta.get("deletionTimestamp") or meta.get("ownerReferences"):
        raise ValueError(f"{namespace}/{kind}/{name} must have a stable, independently owned identity")
    # Only these four fields can be recorded. Anything else is not an adoption patch from this tool.
    ownership = {("labels", "app.kubernetes.io/managed-by"): "Helm", ("annotations", "meta.helm.sh/release-name"): RELEASE,
                 ("annotations", "meta.helm.sh/release-namespace"): namespace, ("annotations", "helm.sh/resource-policy"): "keep"}
    added, adopted_uid = [], None
    for operation in recorded:
        action, path = operation["op"], operation["path"]
        if action == "test" and path in ("/metadata/uid", "/metadata/resourceVersion"):
            adopted_uid = operation["value"] if path == "/metadata/uid" else adopted_uid
            continue
        parts = path.split("/")
        if action != "add" or parts[:2] != ["", "metadata"] or len(parts) not in (3, 4) or parts[2] not in ("labels", "annotations"):
            raise ValueError(f"Unexpected recorded operation for {namespace}/{kind}/{name}")
        fields = operation["value"] if len(parts) == 3 else {parts[3].replace("~1", "/").replace("~0", "~"): operation["value"]}
        if not isinstance(fields, dict) or any(ownership.get((parts[2], field)) != value for field, value in fields.items()):
            raise ValueError(f"Unexpected recorded metadata for {namespace}/{kind}/{name}")
        added.extend((parts[2], field) for field in fields)
    if added and adopted_uid != meta["uid"]:
        raise ValueError(f"{namespace}/{kind}/{name} is not the object that was adopted")
    removals = []
    for section, field in added:
        current = (meta.get(section) or {}).get(field)
        if current is None:
            continue
        if current != ownership[section, field]:
            raise ValueError(f"Ownership metadata of {namespace}/{kind}/{name} changed since adoption")
        pointer = "/metadata/" + section + "/" + field.replace("~", "~0").replace("/", "~1")
        removals += [{"op": "test", "path": pointer, "value": current}, {"op": "remove", "path": pointer}]
    if not removals:
        return []
    return [
        {"op": "test", "path": "/metadata/uid", "value": meta["uid"]},
        {"op": "test", "path": "/metadata/resourceVersion", "value": meta["resourceVersion"]},
        *removals,
    ]


def release_plan(kubeconfig, context, record, namespace=None):
    """Plan the de-adoption of every object in a recorded adoption plan, or of one namespace."""
    allowlist = yaml.safe_load(ALLOWLIST.read_text())
    recorded = json.loads(Path(record).read_text())
    seen = set()
    # Check the whole record before the first read.
    for item in recorded:
        ns, kind, name = item["namespace"], item["kind"], item["name"]
        if name not in allowlist.get(ns, {}).get(kind, []) or (ns, kind, name) in seen:
            raise ValueError("Recorded plan names a duplicate object or one outside the allowlist")
        seen.add((ns, kind, name))
    result = []
    for item in recorded:
        ns, kind, name = item["namespace"], item["kind"], item["name"]
        if namespace in (None, ns):
            patch = release_patch(ns, kind, name, read_live(kubeconfig, context, ns, kind, name), item["patch"])
            result.append({"namespace": ns, "kind": kind, "name": name, "patch": patch})
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", choices=yaml.safe_load(ALLOWLIST.read_text()))
    parser.add_argument("--de-adopt", type=Path, metavar="RECORD",
                        help="Print patches that remove the metadata added by the adoption patches in this record")
    args = parser.parse_args()
    if args.de_adopt:
        result = release_plan(args.kubeconfig, args.context, args.de_adopt, args.namespace)
    else:
        result = plan(args.kubeconfig, args.context, args.namespace)
    print(json.dumps(result, indent=2))
