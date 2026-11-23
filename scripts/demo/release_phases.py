# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Order a direct Helmfile apply by stack and gate each stack on its health checks."""

# Sync upgrades a release whatever its diff, so a failed release is retried with
# sync even when its rendered objects are unchanged.
METHODS = {"deployed": "apply", "failed": "sync"}
# Set only when a named Helm read reports the release not found, never because a listing omits it.
ABSENT = "absent"


def stack_of(release):
    return "oci" if release.startswith("default/") else "otel"


def helm_statuses(listing):
    """Map parsed `helm list --all-namespaces --max 0 -o json` output to {namespace/name: status}.

    Helm 4 lists releases in every status by default. Without --max 0 it stops after 256.
    """
    statuses = {}
    for entry in listing:
        key = entry["namespace"] + "/" + entry["name"]
        if key in statuses:
            raise ValueError(f"{key} appears twice in the Helm release listing")
        statuses[key] = entry["status"]
    return statuses


def selected_statuses(rows, listing, read_status):
    """Return the latest Helm status of every selected release.

    listing is parsed `helm list --all-namespaces --max 0 -o json`. Helm 4's default list
    mask omits a release whose status is unknown, so an omitted release is read by name:
    read_status("namespace/name") returns the status that `helm history <name> -n <namespace>
    --max 1 -o json` reports, or None only when Helm reports the release not found.
    """
    listed = helm_statuses(listing)
    statuses = {}
    for row in rows:
        key = row["id"]
        if key in listed:
            statuses[key] = listed[key]
        else:
            status = read_status(key)
            statuses[key] = ABSENT if status is None else status
    return statuses


def run(rows, statuses, reconcile, verify_stack):
    """Reconcile the selected releases, OCI before OTel. Callbacks raise on failure.

    rows lists the selected releases as {"id": "namespace/name", ...} in execution order.
    statuses maps every selected release to its latest Helm status, or to ABSENT once a named
    read confirmed that Helm has no such release (see selected_statuses). A selected release
    without a status is refused. reconcile(row, method) runs Helmfile for one release.
    verify_stack(stack) returns True only when every required check passes. Every state is
    checked before the first write, and a receipt is returned only after all operations and
    health checks.
    """
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate release selection")
    methods = {}
    for key in ids:
        if key not in statuses:
            # A listing omits some states, so a missing entry proves nothing about the release.
            raise ValueError(f"{key} has no confirmed Helm status")
        if statuses[key] == ABSENT and key.endswith("/demo-resources"):
            # Only a namespace-local companion release may be installed for the first time.
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
