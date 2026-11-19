# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Render the request record, actor binding, and fail-closed admission for review."""

import argparse
import json
import sys
import subprocess
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scheduled_request import CHECKPOINT, HOLDER, PROTOCOL, REQUEST, SCHEMA

GROUP = "rbac.authorization.k8s.io"
ADMISSION = "admissionregistration.k8s.io/v1"
ROLE = "access-daily-hosted-update"
POLICY = "jaeger-reviewed-daily-request"


def recovery_guards(recovery):
    # A recovery identity with separately reviewed checkpoint RBAC can only drop
    # the daily holder for the matching failed/verified request. Manual owners and
    # revision data stay protected even if it bypasses the operator CLI.
    holder, protocol = json.dumps(HOLDER), json.dumps(PROTOCOL)
    unchanged = []
    for field in (
        "data",
        "binaryData",
        "immutable",
        "metadata.labels",
        "metadata.finalizers",
        "metadata.ownerReferences",
    ):
        unchanged.append(
            f"(has(object.{field}) == has(oldObject.{field}) && "
            f"(!has(object.{field}) || object.{field} == oldObject.{field}))"
        )
    lock_check = (
        "params.data.state in ['failed','verified'] && "
        + f"{holder} in variables.before && variables.before[{holder}] == 'daily/' + params.data.slot + '/' + string(params.metadata.uid) && "
        + f"{protocol} in variables.before && variables.before[{protocol}] == 'v1' && !({holder} in variables.after) && "
        + "variables.after.size() + 1 == variables.before.size() && variables.after.all(k, k in variables.before && variables.after[k] == variables.before[k]) && "
        + " && ".join(unchanged)
    )
    unlocked = (
        f"has(params.metadata.annotations) && {protocol} in params.metadata.annotations && "
        f"params.metadata.annotations[{protocol}] == 'v1' && !({holder} in params.metadata.annotations)"
    )
    for suffix, target, parameter, expression, message in (
        (
            "lock",
            CHECKPOINT,
            REQUEST,
            lock_check,
            "Recovery may only remove the matching daily holder after failure or verification.",
        ),
        (
            "result",
            REQUEST,
            CHECKPOINT,
            unlocked,
            "Recovery can finalize the request only after the checkpoint has been unlocked.",
        ),
    ):
        name = f"{POLICY}-recovery-{suffix}"
        spec = {
            "failurePolicy": "Fail",
            "paramKind": {"apiVersion": "v1", "kind": "ConfigMap"},
            "matchConstraints": {
                "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": target[0]}},
                "resourceRules": [
                    {
                        "apiGroups": [""],
                        "apiVersions": ["v1"],
                        "operations": ["UPDATE"],
                        "resources": ["configmaps"],
                        "resourceNames": [target[1]],
                    }
                ],
            },
            "matchConditions": [
                {
                    "name": "recovery-record",
                    "expression": f"request.userInfo.username == {json.dumps(recovery)} && request.namespace == {json.dumps(target[0])} && object.metadata.name == {json.dumps(target[1])}",
                }
            ],
            "validations": [{"expression": expression, "message": message}],
        }
        if target == CHECKPOINT:
            spec["variables"] = [
                {"name": "before", "expression": "oldObject.metadata.annotations"},
                {"name": "after", "expression": "object.metadata.annotations"},
            ]
        yield {
            "apiVersion": ADMISSION,
            "kind": "ValidatingAdmissionPolicy",
            "metadata": {"name": name},
            "spec": spec,
        }
        yield {
            "apiVersion": ADMISSION,
            "kind": "ValidatingAdmissionPolicyBinding",
            "metadata": {"name": name},
            "spec": {
                "policyName": name,
                "validationActions": ["Deny"],
                "paramRef": {
                    "namespace": parameter[0],
                    "name": parameter[1],
                    "parameterNotFoundAction": "Deny",
                },
            },
        }


def manifests(requester, preparer, executor, recovery):
    identities = [requester, preparer, executor, recovery]
    if len(set(identities)) != 4 or any(
        not v or "*" in v or len(v) > 512 or not v.isprintable() for v in identities
    ):
        raise ValueError("Four distinct exact authenticated Kubernetes usernames are required")
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
                for k, v in zip(("requester", "preparer", "executor", "recovery"), identities)
            ],
            "validations": [
                {
                    "expression": "variables.requester || variables.preparer || variables.executor || variables.recovery",
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
(oldObject.data.state in ['succeeded','recovered','ready','requested'] && timestamp(object.data.slot) > timestamp(oldObject.data.slot)))) ||
(variables.executor && ((oldObject.data.state == 'requested' && object.data.state == 'running') ||
(oldObject.data.state == 'running' && object.data.state in ['failed','verified']) ||
(oldObject.data.state == 'verified' && object.data.state == 'succeeded'))) ||
(variables.recovery && ((oldObject.data.state == 'failed' && object.data.state == 'recovered') ||
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
    yield from recovery_guards(recovery)
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
    for actor in ("requester", "preparer", "executor", "recovery"):
        parser.add_argument("--" + actor, required=True)
    parser.add_argument("--phase", choices=["guard", "verified-binding"], default="guard")
    args = parser.parse_args()
    resources = list(manifests(args.requester, args.preparer, args.executor, args.recovery))
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
