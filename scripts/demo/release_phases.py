# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Reconcile a verified, ordered bundle while the executor holds the deployment lock."""

import json


def observation_scope(rows, cluster, images):
    ids = [row["id"] for row in rows]
    stacks = {stack for stack, key in (("oci", "default/jaeger"), ("otel", "jaeger/jaeger")) if key in ids}
    return {
        "cluster": cluster, "releases": ids, "objects": sorted({key for row in rows for key in row["observe"]}),
        "imageStacks": sorted(stacks.intersection(images)),
        "rollbackRevisions": {row["id"]: row["rollback"]["revision"] for row in rows},
    }


def baseline_for(scope, previous):
    state = {k: previous[k] for k in ("cluster", "capabilitiesSHA256", "healthy")}
    for k in ("releases", "rollbackTargets", "objects"):
        ids = scope["objects" if k == "objects" else "releases"]
        state[k] = {key: previous[k][key] for key in ids}
    if "images" in previous:
        state["images"] = {k: previous["images"][k] for k in scope["imageStacks"]}
    return state


def run(plan, previous, read_live, reconcile, verify_stack):
    """Run inside consume() after its full fresh observation. Callbacks raise on failure.

    reconcile(row, method) gets the previously verified release's private local
    inputs from the caller. verify_stack returns True only when every required check passes.
    A receipt is returned only after all selected operations and health checks.
    """
    rows = plan["releases"]
    methods = {"deployed": "apply", "failed": "sync", "absent": "apply"}
    ids = [row["id"] for row in rows]
    if set(ids) != set(previous["releases"]) or len(set(ids)) != len(ids):
        raise ValueError("Selected release scope differs from the verified baseline")
    for row in rows:
        key = row["id"]
        state = previous["releases"][key]["status"]
        if state not in methods or (state == "absent" and not key.endswith("/demo-resources")):
            raise ValueError(f"{key} requires operator review before reconciliation")
    phases = {"oci": [], "otel": []}
    for row in rows:
        stack = "oci" if row["id"].startswith("default/") else "otel"
        phases[stack].append(row)
    if phases["oci"] + phases["otel"] != rows or not rows:
        raise ValueError("Invalid OCI and OTel execution order")
    receipt = {"releases": {}, "verifiedStacks": []}
    for stack, selected in phases.items():
        if selected:
            # Compare only this stack. An earlier successful OCI release may
            # have legitimately advanced its history and changed its objects.
            scope = observation_scope(selected, previous["cluster"], plan["images"])
            expected = baseline_for(scope, previous)
            actual = read_live(scope)
            if json.dumps(actual, sort_keys=True, allow_nan=False) != json.dumps(expected, sort_keys=True, allow_nan=False):
                raise ValueError(f"{stack} changed after the initial bundle verification")
        for row in selected:
            method = methods[previous["releases"][row["id"]]["status"]]
            reconcile(row, method)
            receipt["releases"][row["id"]] = method
        # An OTel-only run must also pass the OCI gate. Both no-op and retry
        # paths verify health before the next phase or a last-known-good receipt.
        if selected or stack == "oci":
            if verify_stack(stack) is not True:
                raise ValueError(f"{stack} verification failed")
            receipt["verifiedStacks"].append(stack)
    return receipt
