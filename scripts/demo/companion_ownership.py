#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Prepare metadata-only ownership patches for the four namespace-local releases."""

import argparse
import json
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


def plan(kubeconfig, context, namespace=None):
    allowlist = yaml.safe_load(ALLOWLIST.read_text())
    result = []
    for ns in ([namespace] if namespace else allowlist):
        for kind, names in allowlist[ns].items():
            for name in names:
                cmd = ["kubectl", "--kubeconfig", kubeconfig, "--context", context, "--namespace", ns,
                       "get", kind, name, "--output=json"]
                resource = json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)
                patch = ownership_patch(ns, kind, name, resource)
                result.append({"namespace": ns, "kind": kind, "name": name, "patch": patch})
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", choices=yaml.safe_load(ALLOWLIST.read_text()))
    args = parser.parse_args()
    print(json.dumps(plan(args.kubeconfig, args.context, args.namespace), indent=2))
