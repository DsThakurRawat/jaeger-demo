# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Order a direct Helmfile apply by stack and gate each stack on its health checks."""

# Sync upgrades a release whatever its diff, so a failed release is retried with
# sync even when its rendered objects are unchanged.
METHODS = {"deployed": "apply", "failed": "sync"}
# Set only when a named Helm read reports the release not found, never because a listing omits it.
ABSENT = "absent"
# The four namespaces that hold releases. Each is listed on its own.
NAMESPACES = ("default", "jaeger", "opensearch", "otel-demo")
COMPANION = "demo-resources"


def stack_of(release):
    return "oci" if release.startswith("default/") else "otel"


def list_args(namespace):
    """Arguments after `helm` that list one namespace's releases in every status.

    Helm 4 lists releases in every status by default. Without --max 0 it stops after 256.
    A listing of one namespace needs only a namespaced Secret list, which the deployer role
    grants. A cluster-wide listing needs a cluster-wide Secret list, which it denies.
    """
    if namespace not in NAMESPACES:
        raise ValueError(f"{namespace} is not a release namespace")
    return ["list", "-n", namespace, "--max", "0", "-o", "json"]


def helm_statuses(listings):
    """Map {namespace: parsed `helm list -n namespace --max 0 -o json`} to {namespace/name: status}."""
    statuses = {}
    for namespace, listing in listings.items():
        for entry in listing:
            if entry["namespace"] != namespace:
                raise ValueError(f"{entry['namespace']}/{entry['name']} appears in the listing of {namespace}")
            key = namespace + "/" + entry["name"]
            if key in statuses:
                raise ValueError(f"{key} appears twice in the Helm release listing")
            statuses[key] = entry["status"]
    return statuses


def selected_statuses(rows, listings, read_status):
    """Return the latest Helm status of every selected release.

    listings maps each release namespace to its parsed `helm list -n namespace --max 0 -o json`
    (see list_args). Helm 4's default list mask omits a release whose status is unknown, so an
    omitted release is read by name: read_status("namespace/name") returns the status that
    `helm history <name> -n <namespace> --max 1 -o json` reports, or None only when Helm
    reports the release not found. A selected release whose namespace has no listing is refused.
    """
    listed = helm_statuses(listings)
    statuses = {}
    for row in rows:
        key = row["id"]
        if key.split("/", 1)[0] not in listings:
            raise ValueError(f"{key} is in a namespace without a Helm release listing")
        if key in listed:
            statuses[key] = listed[key]
        else:
            status = read_status(key)
            statuses[key] = ABSENT if status is None else status
    return statuses


def install_args(namespace, chart, values):
    """Arguments after `helm` for the first install of a companion release.

    A plain install, because Helm's upgrade --install cannot require an absent release,
    whereas install fails on any stored record of the name and refuses an existing object that
    lacks this release's ownership metadata. It is not atomic, so a failure leaves its record
    for review, and it has no --take-ownership. Run it for one namespace at a time.
    """
    if namespace not in NAMESPACES:
        raise ValueError(f"{namespace} is not a release namespace")
    return ["install", COMPANION, str(chart), "--namespace", namespace, "--values", str(values),
            "--wait=legacy", "--timeout", "300s", "--rollback-on-failure=false", "--create-namespace=false"]


def run(rows, statuses, reconcile, verify_stack, install=frozenset()):
    """Reconcile the selected releases, OCI before OTel. Callbacks raise on failure.

    rows lists the selected releases as {"id": "namespace/name", ...} in execution order.
    statuses maps every selected release to its latest Helm status, or to ABSENT once a named
    read confirmed that Helm has no such release (see selected_statuses). A selected release
    without a status is refused. reconcile(row, method) runs Helmfile for one release, or the
    first install for method "install". verify_stack(stack) returns True only when every
    required check passes. Every state is checked before the first write, and a receipt is
    returned only after all operations and health checks.

    install names the companion release to install for the first time. It is honored only when
    that release is the whole selection and Helm confirmed it absent. Any other absent release
    is refused, and so is an absent companion that install does not name.
    """
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate release selection")
    if install:
        if any(key.rsplit("/", 1)[-1] != COMPANION for key in install):
            raise ValueError(f"Only a {COMPANION} companion release can be installed")
        if len(ids) != 1 or set(ids) != set(install):
            raise ValueError("A first install must be the only selected release")
    methods = {}
    for key in ids:
        if key not in statuses:
            # A listing omits some states, so a missing entry proves nothing about the release.
            raise ValueError(f"{key} has no confirmed Helm status")
        if key in install:
            if statuses[key] != ABSENT:
                raise ValueError(f"{key} is not absent and cannot be installed")
            # A plain helm install, because Helmfile apply refuses an absent release.
            methods[key] = "install"
        elif statuses[key] in METHODS:
            methods[key] = METHODS[statuses[key]]
        else:
            hint = " (absent; a companion needs an explicit first install)" if statuses[key] == ABSENT else ""
            raise ValueError(f"{key} requires operator review before reconciliation{hint}")
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
