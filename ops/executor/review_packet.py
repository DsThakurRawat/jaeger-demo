#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Assemble a private, offline production installation proposal. No Kubernetes client is used."""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

import approval_policies
import permissions
import workloads

ROOT = Path(__file__).resolve().parents[2]
REQUEST_SOURCE = ROOT / "scripts/demo/rehearsals/scheduled-request/access.py"
spec = importlib.util.spec_from_file_location("reviewed_daily_request", REQUEST_SOURCE)
access = importlib.util.module_from_spec(spec)
spec.loader.exec_module(access)
STAGES = (
    (
        "00-foundations",
        "Confirm target cluster, network isolation, service accounts and Kubernetes admission support.",
    ),
    ("01-approval-schema", "Wait for the CRD Established condition before creating its typed parameters."),
    (
        "02-bootstrap-records",
        "Create missing records only. Preserve existing request state and previously staged parameters.",
    ),
    ("10-request-guards", "Verify request and recovery policy definitions and bindings. Run the request barrier as the authorized bootstrap user."),
    (
        "11-approval-guards",
        "Verify the fail-closed parameter bindings and dry-run-only policy definitions before staging rights.",
    ),
    (
        "12-workload-guards",
        "Read back the dated holder, exact-state rules and their target bindings.",
    ),
    ("13-history-guards", "Read back the six-release history limits and protected chart Secret policies."),
    (
        "14-monitoring-guards",
        "Use server-side creation or apply for the immutable archive. Verify the twelve admitted hooks.",
    ),
    (
        "20-sensitive-reads",
        "Review effective get/list of every Secret in four namespaces under both accounts.",
    ),
    (
        "21-request-grants",
        "Confirm the bootstrap admission barrier on every serving endpoint. After staging the approved binding, verify the actual request actors.",
    ),
    (
        "22-preparer-grants",
        "With the preparer suspended, verify its named server dry-run rights and the installed policy requiring dryRun.",
    ),
    (
        "23-workload-grants",
        "Verify no-op and staged changes, ownership, foreign changes and checkpoint transitions.",
    ),
    (
        "24-history-grants",
        "Verify canonical six-release Secret envelopes and enforce independent inspection before rollback.",
    ),
    (
        "25-monitoring-grants",
        "Review hook collection creation, named deletion, bind and collection-scoped escalate.",
    ),
    (
        "30-storage",
        "Confirm RWO storage placement, capacity and cost. PVC provisioning may allocate storage.",
    ),
    (
        "31-volume-init",
        "The tokenless initializer starts when this Job is created. Check volume permissions and completion.",
    ),
    (
        "32-inactive-runtime",
        "Verify both key projections, digest, scheduler suspension and zero receiver replicas.",
    ),
)
RBAC = {"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"}
VAP = {"ValidatingAdmissionPolicy", "ValidatingAdmissionPolicyBinding"}
ORDER = {
    kind: i
    for i, kind in enumerate(
        (
            "Namespace",
            "CustomResourceDefinition",
            "ServiceAccount",
            "ConfigMap",
            "DailyWorkloadApproval",
            "ValidatingAdmissionPolicy",
            "ValidatingAdmissionPolicyBinding",
            "Role",
            "ClusterRole",
            "RoleBinding",
            "ClusterRoleBinding",
        )
    )
}
PREREQUISITES = [
    "Confirm the kube-system UID and Kubernetes v1.37.0 from a trusted operator context. Verify the published Linux amd64 executable image digest and provenance.",
    "Verify the four distinct authenticated actor usernames and the different bootstrap operator. Inspect existing, aggregated and inherited rights. The recovery operator separately needs named get/patch on the existing default/jaeger-demo-deployment-revisions checkpoint.",
    "Keep manual writers idle. Confirm the checkpoint UID, version, existing five component fingerprints, protocol annotation v1, and an empty holder. Never replace an existing request, parameter, history or checkpoint with a seed.",
    "Before write bindings, verify policy and parameter readback on each serving API endpoint. Run access.py --phase verified-binding under the authorized bootstrap user and compare the emitted binding. As each binding is staged with runtime actors idle, check effective rights and policy-specific denials under its authentic subject.",
    "Verify the six current releases, pinned successful monitoring configuration, chart-owned data and real restore target. Recheck storage binding, workload UID and independent ready/health evidence.",
    "Establish private key custody out of band. Review namespace tenancy, PVC encryption and retention, RWO scheduling, resources and NetworkPolicy enforcement. No Kubernetes Secret is generated by this packet.",
    "Keep DEMO_SCHEDULE_ENABLED=false during staging. Verify the request-only environment can access SLACK_WEBHOOK_URL by name. Start the cleared receiver, verify readiness and use an authorized one-off preparer in the supported UTC window to obtain the intended ready receipt.",
]
FIRST_RUN = [
    "With explicit activation approval, enable the 12:20 UTC preparer and separately enable the GitHub signal for the intended 13:00 UTC event. Confirm the ready receipt matches the configuration commit.",
    "When the GitHub schedule job actually runs, retain its URL, UTC slot and receipt. Require succeeded, an unlocked checkpoint and the six-release/five-component reconciliation. Check fresh queryable traces, public routing and TLS, storage bindings and finite SPM under observed traffic.",
    "On failure, pause future signals, inspect the held lock and compare the failed run ID with the alert in #jaeger-operations. A successful run sends no failure alert.",
]
SENSITIVE = [
    "The preparer and executor get/list Secrets in default, jaeger, opensearch and otel-demo. Kubernetes list returns full payloads, including unrelated application and TLS credentials.",
    "The executor writes canonical Helm history Secrets. Native admission checks the envelope. A trusted writer and independent rollback verification cover the encoded history contents.",
    "The optional monitoring permissions include cluster-scoped hook RBAC, named bind, collection-level escalate and named deletion. Inspect high-value roles, service accounts, Jobs and admission guards before granting them.",
    "The RWO private PVC holds encrypted release bundles and durable outcomes. Review the two distinct key Secret projections and the executor's read-only bundle mount.",
]


def require(test, message):
    if not test:
        raise ValueError(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encoded(data):
    return json.dumps(data, ensure_ascii=True, sort_keys=True, indent=2, allow_nan=False).encode() + b"\n"


def identity(obj):
    m = obj["metadata"]
    return (
        obj["apiVersion"].partition("/")[0] if "/" in obj["apiVersion"] else "",
        obj["kind"],
        m.get("namespace", ""),
        m["name"],
    )


def reviewed_hooks(directory, attestation, cluster_uid):
    if directory is None:
        require(attestation is None, "Supply the hook directory together with its independent digest record")
        return None
    require(
        isinstance(attestation, dict)
        and set(attestation) == {"clusterUID", "guardsSHA256", "grantsSHA256"}
        and attestation["clusterUID"] == cluster_uid,
        "Hook review requires both digests and the identical independently observed cluster UID",
    )
    result = []
    for item, kinds in (("guards", VAP | {"ConfigMap"}), ("grants", RBAC)):
        filename = directory / (item + ".json")
        expected = attestation[item + "SHA256"]
        require(
            isinstance(expected, str)
            and re.fullmatch(r"[a-f0-9]{64}", expected)
            and not filename.is_symlink()
            and filename.is_file()
            and 0 < filename.stat().st_size <= 8 * 1024 * 1024,
            "Expected a bounded reviewed hook file and SHA-256",
        )
        raw = filename.read_bytes()
        require(sha(raw) == expected, "Hook file differs from its independent review digest")
        doc = json.loads(raw)
        require(
            isinstance(doc, dict)
            and doc.get("apiVersion") == "v1"
            and doc.get("kind") == "List"
            and isinstance(doc.get("items"), list)
            and doc["items"]
            and all(o.get("kind") in kinds for o in doc["items"]),
            "Unexpected kind or missing objects in the reviewed hook phase",
        )
        result.append(doc["items"])
    require(
        any(
            o["kind"] == "ConfigMap"
            and o.get("immutable") is True
            and o["metadata"].get("namespace") == permissions.OPS
            for o in result[0]
        )
        and any(o["kind"] == "ClusterRoleBinding" for o in result[1]),
        "Incomplete monitoring hook package",
    )
    return result


def build(actors, image, storage_class, cluster_uid, hooks):
    require(
        isinstance(actors, dict)
        and set(actors) == {"requester", "preparer", "executor", "recovery", "bootstrap"}
        and all(
            isinstance(v, str) and v == v.strip() and v and v.isprintable() and "*" not in v and len(v) <= 512
            for v in actors.values()
        )
        and len(set(actors.values())) == 5,
        "Supply five distinct, exact authenticated Kubernetes usernames",
    )
    sa = lambda role: f"system:serviceaccount:{permissions.OPS}:{workloads.ACCOUNT[role]}"
    require(
        actors["preparer"] == sa("prepare") and actors["executor"] == sa("serve"),
        "Preparer and executor must match the installed service accounts",
    )
    result = {stage: [] for stage, _ in STAGES}
    namespace = "00-foundations"
    runtime = {
        "Namespace": namespace,
        "ServiceAccount": namespace,
        "NetworkPolicy": namespace,
        "PersistentVolumeClaim": "30-storage",
        "Job": "31-volume-init",
        "Deployment": "32-inactive-runtime",
        "CronJob": "32-inactive-runtime",
    }
    for o in workloads.render(image, storage_class, cluster_uid):
        result[runtime[o["kind"]]].append(o)
    for o in access.manifests(*(actors[k] for k in ("requester", "preparer", "executor", "recovery"))):
        kind = o["kind"]
        stage = (
            namespace
            if kind == "Namespace"
            else "02-bootstrap-records"
            if kind == "ConfigMap"
            else "10-request-guards"
            if kind in VAP
            else "21-request-grants"
        )
        require(kind in {"Namespace", "ConfigMap"} | VAP | RBAC, "Unexpected control object")
        result[stage].append(o)
    for stage, source in (
        ("01-approval-schema", "approval-schema"),
        ("02-bootstrap-records", "approval-seeds"),
        ("11-approval-guards", "approval-guards"),
        ("22-preparer-grants", "approval-grants"),
    ):
        result[stage].extend(approval_policies.stages(source))
    result["12-workload-guards"], result["23-workload-grants"] = permissions.regular(daily=True)
    result["13-history-guards"] = permissions.history_guard()
    result["24-history-grants"] = list(permissions.history_grants())
    result["20-sensitive-reads"] = list(permissions.read_access())
    if hooks:
        result["14-monitoring-guards"], result["25-monitoring-grants"] = hooks
    return normalize(result)


def normalize(stages):
    seen = {}
    duplicates = 0
    for stage, items in stages.items():
        clean = []
        for o in items:
            key = identity(o)
            require(o["kind"] != "Secret", "A review packet cannot contain Secret payloads")
            if key in seen:
                require(seen[key] == o, "The same Kubernetes identity has conflicting definitions")
                duplicates += 1
                continue
            seen[key] = o
            clean.append(o)
        stages[stage] = sorted(clean, key=lambda o: (ORDER.get(o["kind"], 99), identity(o)))
    # Check dependencies after ordering, including namespaced RoleBindings and cross-namespace parameters.
    present = set()
    for stage, items in stages.items():
        for o in items:
            group, kind, ns, name = identity(o)
            if kind in {"RoleBinding", "ClusterRoleBinding"}:
                ref = o["roleRef"]
                require(
                    (ref["apiGroup"], ref["kind"], ns if ref["kind"] == "Role" else "", ref["name"])
                    in present,
                    "RBAC binding precedes or lacks its role",
                )
            if kind == "ValidatingAdmissionPolicyBinding":
                dep = (group, "ValidatingAdmissionPolicy", "", o["spec"]["policyName"])
                require(dep in present, "Admission binding precedes or lacks its policy")
                if "paramRef" in o["spec"]:
                    param = seen[dep]["spec"]["paramKind"]
                    pg = param["apiVersion"].partition("/")[0] if "/" in param["apiVersion"] else ""
                    ref = o["spec"]["paramRef"]
                    param_id = (pg, param["kind"], ref["namespace"], ref["name"])
                    require(
                        param_id in present
                        or param_id == ("", "ConfigMap", "default", permissions.CHECKPOINT),
                        "Unresolved native parameter dependency",
                    )
            if kind == "Deployment":
                require(o["spec"]["replicas"] == 0, "Runtime receiver must start disabled")
            if kind == "CronJob":
                require(o["spec"]["suspend"] is True, "Runtime CronJob must start suspended")
            present.add((group, kind, ns, name))
    return stages, duplicates


def save(directory, actors, image, storage_class, cluster_uid, hook_directory=None, attestation=None):
    hooks = reviewed_hooks(hook_directory, attestation, cluster_uid)
    stages, duplicates = build(actors, image, storage_class, cluster_uid, hooks)
    directory = directory.resolve()
    require(
        not any((d / ".git").exists() for d in (directory, *directory.parents)),
        "Keep the operator packet outside any Git checkout",
    )
    directory.mkdir(mode=0o700)
    index = {
        "format": "jaeger-scheduled-install-review-v1",
        "clusterUID": cluster_uid,
        "image": image,
        "storageClass": storage_class,
        "authenticatedActors": actors,
        "hookAttestation": attestation,
        "allManifestPhasesIncluded": hooks is not None,
        "duplicateIdentitiesRemoved": duplicates,
        "stages": [],
        "livePrerequisites": PREREQUISITES,
        "activationAndFirstRun": FIRST_RUN,
        "sensitiveAccess": SENSITIVE,
    }

    def write(path, raw):
        with os.fdopen(os.open(directory / path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as dest:
            dest.write(raw)

    for stage, gate in STAGES:
        items = stages[stage]
        if not items:
            index["stages"].append(
                {
                    "phase": stage,
                    "objects": 0,
                    "pending": "Attach the two target-specific reviewed hook outputs",
                }
            )
            continue
        filename = stage + ".json"
        raw = encoded({"apiVersion": "v1", "kind": "List", "items": items})
        write(filename, raw)
        index["stages"].append(
            {"phase": stage, "file": filename, "objects": len(items), "sha256": sha(raw), "gate": gate}
        )
    # Write the index last. Only a complete index describes a valid packet.
    index["rendererSources"] = {
        str(p.relative_to(ROOT)): sha(p.read_bytes())
        for p in (
            Path(__file__).resolve(),
            REQUEST_SOURCE,
            Path(permissions.__file__),
            Path(workloads.__file__),
            Path(approval_policies.__file__),
            Path(approval_policies.t.__file__),
            ROOT / "ops/executor/monitoring_hooks.py",
            ROOT / "docs/all-five-chart-access.json",
            ROOT / "ops/executor/chart-archives.json",
            ROOT / "scripts/demo/scheduled_request.py",
            ROOT / "scripts/demo/scheduled_approvals.py",
            ROOT / "scripts/demo/scheduled_adapter.py",
            ROOT / "examples/oci/monitoring-values.yaml",
        )
    }
    raw = encoded(index)
    write("review.json", raw)
    return index, sha(raw)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actors-file", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--storage-class", required=True)
    parser.add_argument("--cluster-uid", required=True)
    parser.add_argument("--hook-directory", type=Path)
    parser.add_argument(
        "--hook-attestation",
        type=Path,
        help="Independent JSON record containing clusterUID, guardsSHA256 and grantsSHA256",
    )
    parser.add_argument("--output", type=Path, required=True, help="New private directory outside Git")
    args = parser.parse_args()
    index, checksum = save(
        args.output,
        json.loads(args.actors_file.read_bytes()),
        args.image,
        args.storage_class,
        args.cluster_uid,
        args.hook_directory,
        json.loads(args.hook_attestation.read_bytes()) if args.hook_attestation else None,
    )
    print(
        json.dumps(
            {
                "objects": sum(s["objects"] for s in index["stages"]),
                "allManifestPhasesIncluded": index["allManifestPhasesIncluded"],
                "reviewSHA256": checksum,
            }
        )
    )
