#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Check real mounted data across Pod replacement in a private kind namespace."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit
from unittest.mock import patch


STS = "opensearch-cluster-single"
POD = STS + "-0"
MOUNT = "/usr/share/opensearch/data"
IMAGE = "docker.io/library/busybox@sha256:a46257e06c82ad85242d0d38077d0a52d9896389ca60a1a59d3463db586e5fa4"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    if not __debug__ or args.receipt.exists() or args.receipt.is_symlink():
        parser.error("Use the default interpreter and a new receipt path.")
    if not args.context.startswith("kind-jaeger-"):
        parser.error("Expected an explicitly named Jaeger kind fixture.")
    cluster = args.context.removeprefix("kind-")
    base = ["kubectl", "--kubeconfig", str(args.kubeconfig.resolve(strict=True)), "--context", args.context]
    ns = "jaeger-storage-fixture-" + secrets.token_hex(5)

    def kube(cmd, body=None):
        return subprocess.check_output([*base, *cmd], text=True, input=body, timeout=155)

    def namespaced(cmd, body=None):
        return kube(["-n", ns, *cmd], body)

    cfg = json.loads(kube(["config", "view", "--minify", "-o", "json"]))
    endpoint = urlsplit(cfg["clusters"][0]["cluster"]["server"])
    assert endpoint.scheme == "https" and endpoint.hostname in {"127.0.0.1", "localhost", "::1"}
    nodes = json.loads(kube(["get", "nodes", "-o", "json"]))["items"]
    assert nodes and all(n["spec"]["providerID"] in {
        f"kind://{provider}/{cluster}/{n['metadata']['name']}" for provider in ["podman", "docker"]}
        for n in nodes)
    storage = json.loads(kube(["get", "storageclass", "standard", "-o", "json"]))
    assert storage["provisioner"] == "rancher.io/local-path" and storage["reclaimPolicy"] == "Delete"

    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "scripts/demo"))
    import deploy

    def read(cmd, **_kwargs):
        assert cmd[:4] == ["kubectl", "-n", "opensearch", "get"] and cmd[4] in ["pod", "pvc"]
        return subprocess.CompletedProcess(cmd, 0, namespaced(cmd[3:]))

    def binding():
        with patch.object(deploy, "run", read):
            return deploy.opensearch_data_binding()

    created = json.loads(kube(["create", "namespace", ns, "-o", "json"]))["metadata"]["uid"]
    try:
        labels = {"app": "fixture-storage"}
        pod = {"metadata": {"labels": labels}, "spec": {
            "automountServiceAccountToken": False, "terminationGracePeriodSeconds": 1,
            "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "fsGroup": 1000,
                                "seccompProfile": {"type": "RuntimeDefault"}},
            "containers": [{"name": "opensearch", "image": IMAGE, "imagePullPolicy": "IfNotPresent",
                            "command": ["sleep", "3600"], "volumeMounts": [{"name": STS, "mountPath": MOUNT}],
                            "resources": {"requests": {"cpu": "2m", "memory": "4Mi"},
                                          "limits": {"cpu": "50m", "memory": "16Mi"}},
                            "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}}}]}}
        workload = {"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": {"name": STS}, "spec": {
            "serviceName": STS, "replicas": 1, "selector": {"matchLabels": labels}, "template": pod,
            "volumeClaimTemplates": [{"metadata": {"name": STS}, "spec": {
                "storageClassName": "standard", "accessModes": ["ReadWriteOnce"],
                "resources": {"requests": {"storage": "64Mi"}}}}]}}
        namespaced(["create", "-f", "-"], json.dumps(workload))
        ready = ["rollout", "status", "statefulset/" + STS, "--timeout=120s"]
        namespaced(ready)
        before = binding()
        pod_uid = namespaced(["get", "pod", POD, "-o", "jsonpath={.metadata.uid}"])
        marker = secrets.token_hex(24)
        namespaced(["exec", POD, "-c", "opensearch", "--", "sh", "-ec",
                    'printf %s "$1" > "$2"', "fixture", marker, MOUNT + "/marker"])
        namespaced(["rollout", "restart", "statefulset/" + STS])
        namespaced(ready)
        after = binding()
        replacement_uid = namespaced(["get", "pod", POD, "-o", "jsonpath={.metadata.uid}"])
        assert before == after and pod_uid != replacement_uid
        assert namespaced(["exec", POD, "-c", "opensearch", "--", "cat", MOUNT + "/marker"]) == marker
        pv = json.loads(kube(["get", "pv", after[2], "-o", "json"]))
        assert pv["spec"]["claimRef"]["uid"] == after[1] and pv["status"]["phase"] == "Bound"
    finally:
        actual = kube(["get", "ns", ns, "-o", "jsonpath={.metadata.uid}"])
        if actual != created:
            raise RuntimeError("Fixture namespace identity changed before cleanup.")
        kube(["delete", "namespace", ns, "--wait=true", "--timeout=120s"])
    # Wait for the local provisioner's Delete reclaim policy to finish.
    kube(["wait", "--for=delete", "pv/" + after[2], "--timeout=90s"])
    record = {"verifiedAt": datetime.now(timezone.utc).isoformat(), "productionAccess": False,
              "environment": "local kind", "storageProvisioner": storage["provisioner"],
              "claimName": after[0], "PVCUIDAndPVPreserved": True, "controllerBoundVolume": True,
              "newPodUID": True, "markerReadAfterReplacement": True, "namespaceAndPVRemoved": True,
              "application": "BusyBox storage fixture; OpenSearch engine and OCI CSI were not exercised"}
    with tempfile.NamedTemporaryFile("w", delete=False, dir=args.receipt.parent) as f:
        json.dump(record, f, indent=2)
        f.write("\n")
    os.replace(f.name, args.receipt)
    print("Verified real claim binding and mounted file through StatefulSet Pod replacement; fixture removed.")


if __name__ == "__main__":
    main()
