# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Installable native controls for independently planned, private daily parameters."""

import json
from pathlib import Path
import sys
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/demo"))
import scheduled_approval_types as t
import permissions as p

API = t.GROUP + "/" + t.VERSION
PREPARER = f"system:serviceaccount:{p.OPS}:{p.PREPARER}"
SPEC = {
    "type": "object",
    "required": ["scope", "slot", "configurationSHA", "bundleSHA256", "requestUID", "changes"],
    "properties": {
        "scope": {"type": "string", "enum": list(t.SCOPES)},
        "slot": {"type": "string", "pattern": "^" + t.DATE + "$"},
        "configurationSHA": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        "bundleSHA256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "requestUID": {"type": "string", "minLength": 1, "maxLength": 64},
    },
}
FREE = {"type": "object", "x-kubernetes-preserve-unknown-fields": True}
SPEC["properties"]["changes"] = {
    "type": "object",
    "maxProperties": t.MAX_CHANGES,
    "additionalProperties": {
        "type": "object",
        "required": ["uid", "variants"],
        "properties": {
            "uid": {"type": "string", "minLength": 1, "maxLength": 64},
            "variants": {
                "type": "array",
                "minItems": 2,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "required": ["metadata", "body"],
                    "properties": {"metadata": FREE, "body": FREE},
                },
            },
        },
    },
}


def schema():
    return [
        p.resource(
            "apiextensions.k8s.io/v1",
            "CustomResourceDefinition",
            t.RESOURCE + "." + t.GROUP,
            spec={
                "group": t.GROUP,
                "scope": "Namespaced",
                "names": {"plural": t.RESOURCE, "singular": "dailyworkloadapproval", "kind": t.KIND},
                "versions": [
                    {
                        "name": t.VERSION,
                        "served": True,
                        "storage": True,
                        "schema": {
                            "openAPIV3Schema": {
                                "type": "object",
                                "required": ["spec"],
                                "properties": {"spec": SPEC},
                            }
                        },
                    }
                ],
            },
        )
    ]


def seeds():
    return [
        p.resource(
            API,
            t.KIND,
            t.name(scope),
            p.OPS,
            spec={
                "scope": scope,
                "slot": "1970-01-01T13:00:00Z",
                "configurationSHA": "0" * 40,
                "bundleSHA256": "0" * 64,
                "requestUID": "unprepared",
                "changes": {},
            },
        )
        for scope in t.SCOPES
    ]


def typed_binding(pair, scope):
    pair[0]["spec"]["paramKind"] = {"apiVersion": API, "kind": t.KIND}
    pair[1]["spec"]["paramRef"]["name"] = t.name(scope)
    return pair


def verify_receipt_guards():
    result = []
    condition = (
        f'request.namespace == "{p.OPS}" && request.name == "daily-hosted-update"'
        ' && object.data.state in ["ready","requested","running"]'
    )
    # Each binding must pass, and the admission controller denies missing parameters.
    for scope in t.SCOPES:
        expr = (
            f'params.spec.scope == "{scope}" && params.spec.requestUID == object.metadata.uid && '
            '["configurationSHA","bundleSHA256","slot"].all(k, dyn(params.spec)[k] == object.data[k])'
        )
        pair = p.policies(
            "jaeger-demo-required-approval-" + scope,
            "",
            "configmaps",
            ["UPDATE"],
            expr,
            "Every daily parameter must match the request UID, slot and authenticated receipt",
            condition=condition,
            running=False,
        )
        pair[0]["spec"]["matchConditions"] = pair[0]["spec"]["matchConditions"][1:]
        result += typed_binding(pair, scope)
    return result


def publication_guard():
    expr = (
        'request.operation == "UPDATE" && request.userInfo.username == '
        + json.dumps(PREPARER)
        + ' && object.spec.scope == oldObject.spec.scope && object.metadata.name == "daily-approved-" + object.spec.scope'
        " && object.spec.requestUID == params.metadata.uid && "
        + p.same_except(
            "oldObject.metadata", "object.metadata", ["resourceVersion", "generation", "managedFields"]
        )
        + ' && (oldObject.spec == object.spec || params.data.state == "empty" || ('
        'params.data.state in ["ready","requested","succeeded","recovered"] && timestamp(object.spec.slot) > timestamp(params.data.slot)'
        ' && (params.data.state in ["succeeded","recovered"] || timestamp(object.spec.slot) >= timestamp(params.data.expiresAt))))'
    )
    pair = p.policies(
        "jaeger-demo-immutable-approval",
        t.GROUP,
        t.RESOURCE,
        ["UPDATE", "DELETE"],
        expr,
        "Only independent preparation may stage the next slot before advertisement",
        running=False,
    )
    pair[0]["spec"]["matchConditions"] = [
        {
            "name": "fixed-partitions",
            "expression": "request.namespace == "
            + json.dumps(p.OPS)
            + " && request.name in "
            + json.dumps([t.name(n) for n in t.SCOPES]),
        }
    ]
    pair[0]["spec"]["matchConstraints"]["resourceRules"][0]["apiVersions"] = [t.VERSION]
    return pair


def preview_controls():
    by = defaultdict(set)
    for scope, g, r, n in t.allowed():
        by[scope, g, r].add(n)
    guards, roles = [], defaultdict(list)
    for (scope, g, r), names in sorted(by.items()):
        roles[scope].append(p.rule(g, r, ["patch"], names))
        tag = t.digest([scope, g, r])[:18]
        pair = p.policies(
            "jaeger-demo-preview-" + tag,
            g,
            r,
            ["UPDATE"],
            "has(request.dryRun) && request.dryRun",
            "Preparer workload patches must use server-side dry-run",
            running=False,
        )
        # Cover all target names in this GVR/namespace, even if a separate role is mistakenly widened.
        pair[0]["spec"]["matchConditions"] = [
            {
                "name": "independent-preparer",
                "expression": "request.userInfo.username == "
                + json.dumps(PREPARER)
                + " && "
                + p.REQUEST_NS
                + " == "
                + json.dumps(scope),
            }
        ]
        del pair[0]["spec"]["paramKind"]
        del pair[1]["spec"]["paramRef"]
        guards += pair
    bindings = []
    for scope, rules in roles.items():
        bindings += p.bindings("jaeger-demo-preview-only", scope, rules, (p.PREPARER,))
    return guards, bindings


def approval_roles():
    names = [t.name(n) for n in t.SCOPES]
    return p.bindings(
        "jaeger-demo-approval-stage",
        p.OPS,
        [p.rule(t.GROUP, t.RESOURCE, ["get", "patch"], names)],
        (p.PREPARER,),
    )


def exact_variant():
    # Admission CEL's dyn maps allow symmetric field enumeration, including custom GVR bodies.
    top = '["apiVersion","kind","metadata","status"]'
    ignored = json.dumps(sorted(t.SYSTEM_META))
    equality = (
        f"dyn(s).all(k,k in {top} || (k in dyn(v.body) && dyn(s)[k] == dyn(v.body)[k])) && "
        "dyn(v.body).all(k,k in dyn(s) && dyn(s)[k] == dyn(v.body)[k]) && "
        f"dyn(s.metadata).all(k,k in {ignored} || (k in dyn(v.metadata) && dyn(s.metadata)[k] == dyn(v.metadata)[k])) && "
        "dyn(v.metadata).all(k,k in dyn(s.metadata) && dyn(s.metadata)[k] == dyn(v.metadata)[k])"
    )
    return (
        "(has(oldObject.status) == has(object.status) && (!has(oldObject.status) || dyn(oldObject.status) == dyn(object.status))) && "
        "variables.id in params.spec.changes && [oldObject,object].all(s, s.metadata.uid == params.spec.changes[variables.id].uid "
        "&& params.spec.changes[variables.id].variants.exists(v, " + equality + "))"
    )


def stages(which):
    extra, roles = preview_controls()
    if which == "approval-schema":
        return schema()
    if which == "approval-seeds":
        return seeds()
    if which == "approval-guards":
        return publication_guard() + verify_receipt_guards() + extra
    if which == "approval-grants":
        return approval_roles() + roles
    raise ValueError("Unknown approval stage")
