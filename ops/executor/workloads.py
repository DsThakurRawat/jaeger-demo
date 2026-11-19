# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Render an inactive executor installation. This tool has no Kubernetes client."""

import argparse
import json
import re
from uuid import UUID

NS = "jaeger-demo-operations"
PREFIX = "jaeger-demo-"
CLAIM = PREFIX + "bundles"
ENTRY = "/opt/jaeger-demo/scripts/demo/scheduled_receiver.py"
ACCOUNT = {"prepare": PREFIX + "preparer", "serve": PREFIX + "executor"}
IMAGE = re.compile(r"ghcr\.io/jaegertracing/jaeger-demo-executor@sha256:[a-f0-9]{64}")
POD_SECURITY = {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001, "fsGroup": 10001,
                "fsGroupChangePolicy": "OnRootMismatch", "seccompProfile": {"type": "RuntimeDefault"}}
SECURITY = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}


def resource(version, kind, name, **fields):
    meta = {"name": name}
    if kind != "Namespace":
        meta["namespace"] = NS
    return {"apiVersion": version, "kind": kind, "metadata": meta, **fields}


def pod(image, mode, cluster_uid):
    init = mode == "init"
    name = PREFIX + "bundle-init" if init else ACCOUNT[mode]
    volumes = [{"name": "private", "persistentVolumeClaim": {"claimName": CLAIM}},
               {"name": "scratch", "emptyDir": {"sizeLimit": "2Gi"}}]
    mounts = [{"name": "scratch", "mountPath": "/tmp"}]
    container = {"name": mode, "image": image, "imagePullPolicy": "IfNotPresent", "command": ["python3"],
                 "securityContext": SECURITY, "resources": {"requests": {"cpu": "100m", "memory": "256Mi",
                 "ephemeral-storage": "256Mi"}, "limits": {"memory": "1Gi", "ephemeral-storage": "2Gi"}},
                 "volumeMounts": mounts}
    spec = {"nodeSelector": {"kubernetes.io/os": "linux", "kubernetes.io/arch": "amd64"},
            "restartPolicy": "Always" if mode == "serve" else "Never", "securityContext": POD_SECURITY,
            "enableServiceLinks": False, "automountServiceAccountToken": not init, "containers": [container],
            "volumes": volumes, "terminationGracePeriodSeconds": 30}
    if init:
        mounts.append({"name": "private", "mountPath": "/var/lib/jaeger-private"})
        container["args"] = ["-c", "from pathlib import Path\nfor name in ('bundles','outcomes'):\n"
                             "    Path('/var/lib/jaeger-private',name).mkdir(mode=0o700,exist_ok=True)"]
    else:
        spec["serviceAccountName"] = name
        container["env"] = [{"name": "SCHEDULED_CLUSTER_UID", "value": cluster_uid}]
        keys = ["verification-key.pem"] + (["decryption-key.pem"] if mode == "serve" else ["recipient-public.pem", "signing-key.pem"])
        volumes.append({"name": "keys", "secret": {"secretName": name + "-keys", "defaultMode": 0o440,
                        "items": [{"key": k, "path": k} for k in keys]}})
        mounts.extend([{"name": "private", "subPath": "bundles", "mountPath": "/var/lib/jaeger-bundles", "readOnly": mode == "serve"},
                       {"name": "keys", "mountPath": "/var/run/jaeger-bundle-keys", "readOnly": True}])
        container["args"] = [ENTRY, mode]
        if mode == "serve":
            mounts.append({"name": "private", "subPath": "outcomes", "mountPath": "/var/lib/jaeger-outcomes"})
            container["readinessProbe"] = {"exec": {"command": ["python3", ENTRY, "ready"]},
                                           "periodSeconds": 15, "timeoutSeconds": 5, "failureThreshold": 3}
        else:
            spec["affinity"] = {"podAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": [
                {"topologyKey": "kubernetes.io/hostname", "labelSelector": {"matchLabels": {"app": ACCOUNT["serve"]}}}]}}
    return {"metadata": {"labels": {"app": name}}, "spec": spec}


def render(image, storage_class, cluster_uid):
    if str(UUID(cluster_uid)) != cluster_uid:
        raise ValueError("Use the canonical kube-system UID from the intended cluster")
    if not IMAGE.fullmatch(image) or not re.fullmatch(r"[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?", storage_class):
        raise ValueError("Use the published executor image digest and an explicit StorageClass name")
    yield resource("v1", "Namespace", NS)
    for name in ACCOUNT.values():
        yield resource("v1", "ServiceAccount", name, automountServiceAccountToken=False)
    yield resource("networking.k8s.io/v1", "NetworkPolicy", PREFIX + "private",
                   spec={"podSelector": {"matchExpressions": [{"key": "app", "operator": "In",
                         "values": [*ACCOUNT.values(), PREFIX + "bundle-init"]}]},
                         "policyTypes": ["Ingress", "Egress"], "ingress": [], "egress": [
                             {"ports": [{"protocol": proto, "port": 53} for proto in ("UDP", "TCP")]},
                             {"ports": [{"protocol": "TCP", "port": port} for port in (443, 6443)]},
                             {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "opensearch"}}}],
                              "ports": [{"protocol": "TCP", "port": 9200}]},
                             {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "jaeger"}}}],
                              "ports": [{"protocol": "TCP", "port": port} for port in (4318, 16686)]},
                         ]})
    yield resource("v1", "PersistentVolumeClaim", CLAIM, spec={"accessModes": ["ReadWriteOnce"],
                   "storageClassName": storage_class, "resources": {"requests": {"storage": "50Gi"}}})
    yield resource("batch/v1", "Job", PREFIX + "bundle-init", spec={"backoffLimit": 1, "activeDeadlineSeconds": 600,
                   "template": pod(image, "init", cluster_uid)})
    yield resource("apps/v1", "Deployment", ACCOUNT["serve"], spec={"replicas": 0, "strategy": {"type": "Recreate"},
                   "selector": {"matchLabels": {"app": ACCOUNT["serve"]}}, "template": pod(image, "serve", cluster_uid)})
    yield resource("batch/v1", "CronJob", ACCOUNT["prepare"], spec={"schedule": "20 12 * * *", "timeZone": "Etc/UTC",
                   "suspend": True, "concurrencyPolicy": "Forbid", "startingDeadlineSeconds": 900,
                   "successfulJobsHistoryLimit": 2, "failedJobsHistoryLimit": 3,
                   "jobTemplate": {"spec": {"backoffLimit": 0, "activeDeadlineSeconds": 2400, "template": pod(image, "prepare", cluster_uid)}}})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--storage-class", required=True)
    parser.add_argument("--cluster-uid", required=True, help="Independently verified kube-system namespace UID")
    args = parser.parse_args()
    print(json.dumps({"apiVersion": "v1", "kind": "List", "items": list(render(args.image, args.storage_class, args.cluster_uid))}, indent=2))
