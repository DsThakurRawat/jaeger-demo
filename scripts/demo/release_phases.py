# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Order a direct Helmfile apply by stack and gate each stack on its health checks."""

# Sync upgrades a release whatever its diff, so a failed release is retried with
# sync even when its rendered objects are unchanged.
METHODS = {"deployed": "apply", "failed": "sync"}


def stack_of(release):
    return "oci" if release.startswith("default/") else "otel"


def helm_statuses(listing):
    """Map parsed `helm list --all --all-namespaces -o json` output to {namespace/name: status}."""
    statuses = {}
    for entry in listing:
        key = entry["namespace"] + "/" + entry["name"]
        if key in statuses:
            raise ValueError(f"{key} appears twice in the Helm release listing")
        statuses[key] = entry["status"]
    return statuses


def run(rows, statuses, reconcile, verify_stack):
    """Reconcile the selected releases, OCI before OTel. Callbacks raise on failure.

    rows lists the selected releases as {"id": "namespace/name", ...} in execution order.
    statuses maps each existing release to its latest Helm status; a release missing from
    it is absent. reconcile(row, method) runs Helmfile for one release. verify_stack(stack)
    returns True only when every required check passes. Every state is checked before the
    first write, and a receipt is returned only after all operations and health checks.
    """
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate release selection")
    methods = {}
    for key in ids:
        if key not in statuses:
            # Only a namespace-local companion release may be installed for the first time.
            if not key.endswith("/demo-resources"):
                raise ValueError(f"{key} requires operator review before reconciliation")
            methods[key] = "apply"
        elif statuses[key] in METHODS:
            methods[key] = METHODS[statuses[key]]
        else:
            raise ValueError(f"{key} requires operator review before reconciliation")
    phases = {"oci": [], "otel": []}
    for row in rows:
        phases[stack_of(row["id"])].append(row)
    if phases["oci"] + phases["otel"] != rows or not rows:
        raise ValueError("Invalid OCI and OTel execution order")
    receipt = {"releases": {}, "verifiedStacks": []}
    for stack, selected in phases.items():
        for row in selected:
            reconcile(row, methods[row["id"]])
            receipt["releases"][row["id"]] = methods[row["id"]]
        # An OTel-only run must also pass the OCI gate. Both no-op and retry
        # paths verify health before the next phase or a successful receipt.
        if selected or stack == "oci":
            if verify_stack(stack) is not True:
                raise ValueError(f"{stack} verification failed")
            receipt["verifiedStacks"].append(stack)
    return receipt
