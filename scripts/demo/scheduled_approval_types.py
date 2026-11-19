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
MAX_PART = 640 * 1024
MAX_CHANGES = 100
SYSTEM_META = {"resourceVersion", "managedFields", "generation", "creationTimestamp", "uid"}
EXCLUDE = {"secrets", "serviceaccounts", "roles", "rolebindings", "alertmanagers", "prometheuses", "jobs"}
DATE = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T13:00:00Z"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def name(scope):
    if scope not in SCOPES:
        raise ValueError("Unknown workload approval namespace")
    return "daily-approved-" + scope


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


def variant(obj):
    return {
        "body": {k: v for k, v in obj.items() if k not in {"apiVersion", "kind", "metadata", "status"}},
        "metadata": {k: v for k, v in obj["metadata"].items() if k not in SYSTEM_META},
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
