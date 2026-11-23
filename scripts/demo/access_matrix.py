#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Compare the pinned chart renders with the reviewed access matrix.

The daily executor refuses a preparation when a chart renders an object that
docs/all-five-chart-access.json does not list ("Chart object outside reviewed
access inventory"). That message names neither the object nor the fix. This
check renders the six pinned releases without cluster access, names every
object the matrix lacks, lists records that no pinned chart renders any more,
and can draft the missing records for review.

Only object identities are printed: kind, API group, namespace and name. A
client-side render has no live discovery and no Helm lookup results, so this
compares names, kinds and scope, not manifests.
"""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

import scheduled_adapter as a
import scheduled_inputs


MATRIX = "docs/all-five-chart-access.json"
PROCEDURE = "docs/deployer-access.md#changing-the-role"
FIELDS = ("component", "release", "scope", "apiGroup", "resource", "name")
# scheduled_adapter.inventory matches a record with the same rule. The test
# suite checks every reviewed record against both, so the two cannot drift.
PLURALS = {"Ingress": "ingresses", "Prometheus": "prometheuses"}
TEST_HOOKS = {"test", "test-success"}
HOOK_VERBS = ["get", "create", "delete"]
STEADY_VERBS = ["get", "patch"]


def fixture_images():
    """Stand-ins for the two daily snapshot images. Only their shape reaches the render."""
    images = {}
    for role, repository, letter in (("jaeger", "jaegertracing/jaeger-snapshot", "a"),
                                     ("hotrod", "jaegertracing/example-hotrod-snapshot", "b")):
        images[role] = {"repository": repository, "tag": letter * 40, "digest": "sha256:" + letter * 64}
    return images


def row_key(key, ident, supplemental):
    """The matrix fields that identify a rendered object of one release."""
    api, kind, namespace, name = ident.split(":")
    return (a.component(key), "supplement" if supplemental else a.RELEASES[key][1],
            "cluster" if namespace == "-" else namespace, api.partition("/")[0] if "/" in api else "",
            PLURALS.get(kind, kind.lower() + "s"), name)


def hook_events(obj):
    return (obj["metadata"].get("annotations") or {}).get(a.HELM_HOOK, "")


def rendered_rows(key, objects, supplements, reviewed=()):
    """Map each object the executor would check to its matrix key.

    A scheduled upgrade does not run Helm test hooks, so a test-only object needs
    no record. One that already has a record is kept: its hook events changed,
    and that has to be reviewed like any other hook change.
    """
    namespace = a.RELEASES[key][0]
    found = {}
    for obj in objects:
        hook = hook_events(obj)
        row = row_key(key, a.identity(obj, namespace), False)
        if hook and set(hook.split(",")) <= TEST_HOOKS and row not in reviewed:
            continue
        found[row] = {"kind": obj["kind"], "hook": hook}
    for obj in supplements:
        found[row_key(key, a.identity(obj, namespace), True)] = {"kind": obj["kind"], "hook": ""}
    return found


def chart_rows(records):
    """Records for chart and supplemental objects. Helm history and the checkpoint are not rendered."""
    components = {a.component(key) for key in a.RELEASES}
    return {tuple(row[field] for field in FIELDS): row for row in records if row["component"] in components}


def compare(found, records):
    """Return (missing, hook_differences, unused) between the renders and the reviewed records."""
    reviewed = chart_rows(records)
    missing = sorted(key for key in found if key not in reviewed)
    hooks = sorted(key for key in found if key in reviewed
                   and set(filter(None, found[key]["hook"].split(","))) != set(filter(None, reviewed[key].get("hookEvents", "").split(","))))
    unused = sorted(key for key in reviewed if key not in found)
    return missing, hooks, unused


def describe(key):
    component, release, scope, group, resource, name = key
    return f"{component} / {release}: {group or 'core'}/{resource} {scope}/{name}"


def proposal(key, info):
    """A draft record in the matrix's field order. A person decides whether to grant it."""
    row = dict(zip(FIELDS, key))
    if info["hook"]:
        row.update(verbs=HOOK_VERBS, source="pinned Helm upgrade render", hookEvents=info["hook"])
    elif row["release"] == "supplement":
        row.update(verbs=STEADY_VERBS, source="kubectl apply")
    else:
        row.update(verbs=STEADY_VERBS,
                   source="reviewed Jaeger render" if row["release"] == "jaeger" else "pinned Helm upgrade render")
    return row


def packages(charts, root):
    """Locate the archive of each release in a directory staged by scheduled_inputs.charts_for."""
    pins = {release: (variable, name) for release, variable, name, _ in scheduled_inputs.CHARTS}
    located = {}
    for key, (namespace, release, _) in a.RELEASES.items():
        if namespace + "/" + release in pins:
            variable, name = pins[namespace + "/" + release]
            path = charts / f"{name}-{scheduled_inputs.pin(root, variable)}.tgz"
        else:
            packaged = sorted(charts.glob("jaeger-*.tgz"))
            if len(packaged) != 1:
                raise ValueError(f"Expected one packaged Jaeger chart in {charts}, found {len(packaged)}")
            path = packaged[0]
        if not path.is_file():
            raise ValueError(f"Missing chart archive {path.name} in {charts}")
        located[key] = path
    return located


def render(key, package, root, kube_version, env):
    """Render one release as an upgrade, the way the access matrix was derived."""
    namespace, release, _ = a.RELEASES[key]
    values = a.values_for(key, root, fixture_images())
    if key == "oci-monitoring":
        # The chart reads the live Grafana Secret through a Helm lookup.
        a.override(values, ("grafana", "adminPassword"), "synthetic-local-fixture")
    arguments = ["helm", "template", release, str(package), "-n", namespace, f"--kube-version={kube_version}",
                 "--is-upgrade", "--values", "-"]
    try:
        raw = a.command(arguments, body=a.encode(values), env=env)
    except a.AdapterError:
        # Helm output is discarded because a render can contain Secrets.
        raise ValueError(f"helm template failed for {key} with {package.name}. The values in examples/{a.VALUES[key]} "
                         "probably do not fit this chart version; render the chart by hand to see Helm's message") from None
    return a.docs(raw)


def gather(root, charts, kube_version, env):
    """Return {release key: (chart objects, supplemental objects)} for the pinned configuration."""
    return {key: (render(key, package, root, kube_version, env), a.supplemental_for(key, root))
            for key, package in packages(charts, root).items()}


def check(root, renders, out):
    matrix = json.loads((root / MATRIX).read_bytes())
    reviewed = chart_rows(matrix["records"])
    found, counts = {}, []
    for key, (objects, supplements) in renders.items():
        rows = rendered_rows(key, objects, supplements, reviewed)
        if found.keys() & rows.keys():
            raise ValueError("Two releases render the same reviewed identity")
        found.update(rows)
        counts.append(f"  {key}: {len(rows) - len(supplements)} chart objects, {len(supplements)} supplemental")
    missing, hooks, unused = compare(found, matrix["records"])
    print("Compared the pinned renders with " + MATRIX + ":", file=out)
    print("\n".join(counts), file=out)
    if unused:
        print(f"\n{len(unused)} reviewed record(s) match no rendered object. A scheduled run refuses the removal until a\n"
              "manual upgrade has established the new release. Remove the records once no rollback target needs them:", file=out)
        for key in unused:
            print("  - " + describe(key), file=out)
    if hooks:
        print(f"\n{len(hooks)} object(s) have Helm hook events that differ from their record:", file=out)
        for key in hooks:
            print(f"  - {describe(key)}: rendered '{found[key]['hook']}'", file=out)
    if missing:
        print(f"\n{len(missing)} rendered object(s) have no reviewed record:", file=out)
        for key in missing:
            protected = " (PROTECTED kind: grants or carries privilege)" if found[key]["kind"] in a.PROTECTED else ""
            print(f"  - {describe(key)}{protected}", file=out)
    return found, missing, hooks


def main(argv=None, out=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--charts", type=Path,
                        help="directory with the four pinned archives and the packaged Jaeger chart; fetched and verified when omitted")
    parser.add_argument("--propose", action="store_true", help="print draft records for the missing objects as JSON")
    parser.add_argument("--root", type=Path, default=a.ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    root = args.root
    kube_version = json.loads((root / MATRIX).read_bytes())["targetKubernetes"]
    with tempfile.TemporaryDirectory(prefix="access-matrix-") as scratch:
        temp = Path(scratch)
        charts = args.charts
        if charts is None:
            staging = temp / "sources"
            staging.mkdir()
            scheduled_inputs.charts_for(root, staging, scheduled_inputs.public_env(staging))
            charts = staging / "charts"
        env = {"PATH": os.environ["PATH"], "KUBECONFIG": os.devnull, "HOME": scratch}
        try:
            renders = gather(root, charts, kube_version, env)
            found, missing, hooks = check(root, renders, out)
        except (ValueError, a.AdapterError) as error:
            print("error: " + str(error), file=sys.stderr)
            return 2
    if args.propose and missing:
        print("\nDraft records for review. Verbs follow the existing records; confirm each one:", file=out)
        print(json.dumps([proposal(key, found[key]) for key in missing], indent=2), file=out)
    if missing or hooks:
        print("\nThe pinned charts and " + MATRIX + " differ. Run\n"
              "  python3 scripts/demo/access_matrix.py --propose\n"
              "for draft records, review each one, and edit the matrix by hand. Then run\n"
              "scripts/demo/deployer_access_test.py: it fails when ops/rbac/deployer.yaml does not\n"
              "cover the new objects. A change to that role is an access change that an\n"
              "administrator applies before the next deployment: " + PROCEDURE, file=sys.stderr)
        return 1
    print("\nEvery rendered object has a reviewed record.", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
