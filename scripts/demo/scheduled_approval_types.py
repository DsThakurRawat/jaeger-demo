# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Private, named admission parameters shared by the preparer and installer."""

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OPS = "jaeger-demo-operations"
GROUP = "demo.jaegertracing.io"
VERSION = "v1alpha1"
KIND = "DailyWorkloadApproval"
RESOURCE = "dailyworkloadapprovals"
SCOPES = ("default", "jaeger", "opensearch", "otel-demo", "kube-system")
DEFAULT_BUCKETS = 8
DEPLOYMENT_REVISION = "deployment.kubernetes.io/revision"
PARTITIONS = tuple(f"default-{n}" for n in range(DEFAULT_BUCKETS)) + SCOPES[1:]
MAX_PART = 640 * 1024
MAX_CHANGES = 100
SYSTEM_META = {"resourceVersion", "managedFields", "generation", "creationTimestamp", "uid"}
EXCLUDE = {"secrets", "serviceaccounts", "roles", "rolebindings", "alertmanagers", "prometheuses", "jobs"}
DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T13:00:00Z"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def partition(scope, handle):
    if scope not in SCOPES:
        raise ValueError("Unknown approval namespace")
    if scope != "default":
        return scope
    prefix = hashlib.sha256(handle.encode()).digest()[:4]
    return f"default-{int.from_bytes(prefix, 'big') % DEFAULT_BUCKETS}"


def namespace(part):
    if part not in PARTITIONS:
        raise ValueError("Unknown workload approval partition")
    return "default" if part.startswith("default-") else part


def name(part):
    namespace(part)
    return "daily-approved-" + part


def eligible(row):
    return (
        row["scope"] in SCOPES
        and row["resource"] not in EXCLUDE
        and "patch" in row["verbs"]
        and not row.get("hookEvents")
        and row["name"] != "jaeger-demo-deployment-revisions"
        and not (
            row["component"] == "oci-monitoring"
            and row["resource"] in {"deployments", "daemonsets", "statefulsets"}
        )
    )


def allowed():
    rows = json.loads((ROOT / "docs/all-five-chart-access.json").read_bytes())["records"]
    return {(r["scope"], r["apiGroup"], r["resource"], r["name"]) for r in rows if eligible(r)}


def workload_metadata(obj):
    metadata = obj["metadata"]
    if (obj.get("apiVersion"), obj.get("kind")) != ("apps/v1", "Deployment") or "annotations" not in metadata:
        return metadata
    # The Deployment controller advances the revision independently of Helm.
    # An empty annotation map and an absent map encode the same Kubernetes state.
    annotations = {k: v for k, v in (metadata["annotations"] or {}).items() if k != DEPLOYMENT_REVISION}
    rest = {k: v for k, v in metadata.items() if k != "annotations"}
    if annotations:
        rest["annotations"] = annotations
    return rest


def variant(obj):
    return {
        "body": {k: v for k, v in obj.items() if k not in {"apiVersion", "kind", "metadata", "status"}},
        "metadata": {k: v for k, v in workload_metadata(obj).items() if k not in SYSTEM_META},
    }


def validate_receipt(slot, receipt):
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"configurationSHA", "bundleSHA256"}
        or not re.fullmatch(DATE, slot)
        or not re.fullmatch("[0-9a-f]{40}", receipt["configurationSHA"])
        or not re.fullmatch("[0-9a-f]{64}", receipt["bundleSHA256"])
    ):
        raise ValueError("Invalid daily slot or authenticated public receipt")
