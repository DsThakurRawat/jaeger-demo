# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Render the request record, three bindings, and fail-closed admission for review."""

import argparse
import json
import sys
import subprocess
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scheduled_request import REQUEST, SCHEMA

GROUP = "rbac.authorization.k8s.io"
ADMISSION = "admissionregistration.k8s.io/v1"
ROLE = "access-daily-hosted-update"
POLICY = "jaeger-reviewed-daily-request"


def manifests(requester, preparer, executor):
    identities = [requester, preparer, executor]
    if len(set(identities)) != 3 or any(
        not v or "*" in v or len(v) > 512 or not v.isprintable() for v in identities
    ):
        raise ValueError("Three distinct exact authenticated Kubernetes usernames are required")
    ns, name = REQUEST
    nsmeta = {"namespace": ns, "name": ROLE}
    yield {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}}
    yield {
        "apiVersion": ADMISSION,
        "kind": "ValidatingAdmissionPolicy",
        "metadata": {"name": POLICY},
        "spec": {
            "failurePolicy": "Fail",
            "matchConstraints": {
                "resourceRules": [
                    {
                        "apiGroups": [""],
                        "apiVersions": ["v1"],
                        "operations": ["UPDATE"],
                        "resources": ["configmaps"],
                    }
                ]
            },
            "matchConditions": [
                {
                    "name": "control-record",
                    "expression": f"request.namespace == {json.dumps(ns)} && object.metadata.name == {json.dumps(name)}",
                }
            ],
            "variables": [
                {"name": k, "expression": "request.userInfo.username == " + json.dumps(v)}
                for k, v in zip(("requester", "preparer", "executor"), identities)
            ],
            "validations": [
                {
                    "expression": "variables.requester || variables.preparer || variables.executor",
                    "message": "Only a bound daily update actor may change the request.",
                },
                {
                    "expression": "!has(object.binaryData) && !has(object.immutable) && !has(object.metadata.finalizers) && !has(object.metadata.ownerReferences) && "
                    + " && ".join(
                        f"(has(object.metadata.{k}) == has(oldObject.metadata.{k}) && (!has(object.metadata.{k}) || object.metadata.{k} == oldObject.metadata.{k}))"
                        for k in ("annotations", "labels")
                    ),
                    "message": "Request metadata and binary inputs are fixed.",
                },
                {
                    "expression": f"object.data.size() == 8 && object.data.schema == {json.dumps(SCHEMA)} && "
                    + "object.data.engine in ['scripts-v1','helmfile-v1'] && object.data.configurationSHA.matches('^[0-9a-f]{40}$') && "
                    + "object.data.bundleSHA256.matches('^[0-9a-f]{64}$') && "
                    + "object.data.slot.matches('^[0-9]{4}-[0-9]{2}-[0-9]{2}T13:00:00Z$') && "
                    + "timestamp(object.data.expiresAt) > timestamp(object.data.slot) && "
                    + "timestamp(object.data.expiresAt) <= timestamp(object.data.slot) + duration('6h') && "
                    + "(object.data.state == 'ready' ? object.data.requestedAt == '' : "
                    + "(timestamp(object.data.requestedAt) >= timestamp(object.data.slot) && timestamp(object.data.requestedAt) < timestamp(object.data.expiresAt)))",
                    "message": "A request must contain the dated slot, bounded expiry, engine and immutable receipt.",
                },
                {
                    "expression": "variables.preparer || (oldObject.data.size() == object.data.size() && "
                    + "oldObject.data.all(k, k in object.data && (k == 'state' || (variables.requester && k == 'requestedAt') || oldObject.data[k] == object.data[k])))",
                    "message": "Only the independent preparer may set operation inputs.",
                },
                {
                    "expression": """(variables.requester && oldObject.data.state == 'ready' && object.data.state == 'requested') ||
(variables.preparer && object.data.state == 'ready' && (oldObject.data.state == 'empty' ||
(oldObject.data.state in ['succeeded','ready','requested'] && timestamp(object.data.slot) > timestamp(oldObject.data.slot)))) ||
(variables.executor && ((oldObject.data.state == 'requested' && object.data.state == 'running') ||
(oldObject.data.state == 'running' && object.data.state in ['failed','verified']) ||
(oldObject.data.state == 'verified' && object.data.state == 'succeeded')))""",
                    "message": "This authenticated actor cannot perform the requested state transition.",
                },
            ],
        },
    }
    yield {
        "apiVersion": ADMISSION,
        "kind": "ValidatingAdmissionPolicyBinding",
        "metadata": {"name": POLICY},
        "spec": {"policyName": POLICY, "validationActions": ["Deny"]},
    }
    yield {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": name, "namespace": ns},
        "data": {"schema": SCHEMA, "state": "empty"},
    }
    yield {
        "apiVersion": GROUP + "/v1",
        "kind": "Role",
        "metadata": nsmeta,
        "rules": [
            {
                "apiGroups": [""],
                "resources": ["configmaps"],
                "resourceNames": [name],
                "verbs": ["get", "patch"],
            }
        ],
    }
    yield {
        "apiVersion": GROUP + "/v1",
        "kind": "RoleBinding",
        "metadata": nsmeta,
        "roleRef": {"kind": "Role", "apiGroup": GROUP, "name": ROLE},
        "subjects": [{"kind": "User", "apiGroup": GROUP, "name": username} for username in identities],
    }


def barrier_command():
    namespace, name = REQUEST
    return [
        "--request-timeout=10s",
        "-n",
        namespace,
        "patch",
        "configmap",
        name,
        "--type=merge",
        "-p",
        json.dumps({"data": {"unreviewed": "barrier-probe"}}),
        "--dry-run=server",
        "-o",
        "name",
    ]


def verify_barrier(call):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = call()
        if (
            result.returncode
            and POLICY in result.stderr
            and "Only a bound daily update actor" in result.stderr
        ):
            return
        if result.returncode:
            raise RuntimeError("Admission readback did not return the expected policy/actor denial")
        time.sleep(0.25)
    raise RuntimeError("Admission policy was not enforced before the timeout")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for actor in ("requester", "preparer", "executor"):
        parser.add_argument("--" + actor, required=True)
    parser.add_argument("--phase", choices=["guard", "verified-binding"], default="guard")
    args = parser.parse_args()
    resources = list(manifests(args.requester, args.preparer, args.executor))
    bindings = args.phase == "verified-binding"
    if bindings:
        verify_barrier(
            lambda: subprocess.run(
                ["kubectl", *barrier_command()], capture_output=True, text=True, timeout=15
            )
        )
    print(
        json.dumps(
            {
                "apiVersion": "v1",
                "kind": "List",
                "items": [o for o in resources if (o["kind"] == "RoleBinding") == bindings],
            },
            indent=2,
        )
    )
