#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Keep ops/rbac/chart-objects.json equal to what the six pinned releases render.

scripts/demo/deployer_access.py derives the deployer's Kubernetes access from the
object inventory in ops/rbac/chart-objects.json. A chart bump that renders a new
object would otherwise fail at deployment with a Forbidden error, after the
change had merged. `--check` renders the six releases the way the deployment
scripts install them, without cluster access, and exits non-zero when an
object has no record or a record's Helm hook events differ. `--write` adds the
missing records and corrects the hook events.

Only object identities are printed: kind, API group, namespace and name. A
client-side render has no live discovery and no Helm lookup results, so this
compares names, kinds and scope, not manifests. A record that no pinned chart
renders any more is reported and never removed: Helm deletes the object on the
next upgrade, which needs the access, and a rollback target may still hold it.
Remove such a record by hand once no release needs it.
"""

import argparse
import json
from pathlib import Path
import sys
import tempfile

import pr_render_diff as render_diff


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "ops/rbac/chart-objects.json"
TOOLS = Path(__file__).with_name("helmfile-tools.lock.json")
PROCEDURE = "docs/deployer-access.md#changing-the-role"
FIELDS = ("component", "release", "scope", "apiGroup", "resource", "name")
# The release each Helm release name and namespace belongs to in the inventory.
COMPONENTS = {
    ("default", "prometheus"): "oci-monitoring", ("default", "jaeger"): "oci-jaeger",
    ("opensearch", "opensearch"): "opensearch", ("opensearch", "opensearch-dashboards"): "opensearch",
    ("jaeger", "jaeger"): "otel-jaeger", ("otel-demo", "otel-demo"): "otel-demo",
}
SUPPLEMENT = "supplement"
CLUSTER = {"Namespace", "ClusterRole", "ClusterRoleBinding", "ValidatingWebhookConfiguration",
           "MutatingWebhookConfiguration", "CustomResourceDefinition"}
# The kinds that grant or carry privilege. A person decides each one.
PROTECTED = CLUSTER | {"Role", "RoleBinding", "Secret", "ServiceAccount", "Prometheus", "Alertmanager"}
PLURALS = {"Ingress": "ingresses", "Prometheus": "prometheuses"}
HELM_HOOK = "helm.sh/hook"
TEST_HOOKS = {"test", "test-success"}
HOOK_VERBS = ["get", "create", "delete"]
STEADY_VERBS = ["get", "patch"]


def kube_version(tools=TOOLS):
    """The Kubernetes version the pinned toolchain validates against, without its leading v."""
    return json.loads(Path(tools).read_text())["kubernetesSchema"]["version"].lstrip("v")


def hook_events(obj):
    return (obj["metadata"].get("annotations") or {}).get(HELM_HOOK, "")


def events(text):
    return set(filter(None, text.split(",")))


def row_key(namespace, release, obj):
    """The record fields that identify one rendered object."""
    group, _, version = obj["apiVersion"].rpartition("/")
    kind, name = obj["kind"], obj["metadata"]["name"]
    scope = "cluster" if kind in CLUSTER else obj["metadata"].get("namespace", namespace)
    resource = PLURALS.get(kind, kind.lower() + "s")
    if release == "demo-resources":
        # Supplemental objects are not part of a Helm release. A record names the
        # component that applies them, which the render cannot tell apart, so
        # supplemental records are compared by identity alone.
        return ("", SUPPLEMENT, scope, group, resource, name)
    return (COMPONENTS[namespace, release], release, scope, group, resource, name)


def collector(found, reviewed):
    """An indexer for pr_render_diff.render that records identities and hook events in `found`.

    A scheduled upgrade does not run Helm test hooks, so a test-only object needs
    no record. One that already has a record is kept: its hook events changed,
    and that has to be reviewed like any other hook change.
    """
    def index(_, namespace, release, docs):
        for obj in docs:
            if obj is None:
                continue
            key = row_key(namespace, release, obj)
            hook = hook_events(obj)
            if hook and events(hook) <= TEST_HOOKS and key not in reviewed:
                continue
            if key in found:
                raise ValueError("Two renders produce the same object identity")
            found[key] = {"kind": obj["kind"], "hook": hook}
    return index


def reviewed_rows(records):
    """Records for chart and supplemental objects. Helm history and the checkpoint are not rendered."""
    rows = {}
    for row in records:
        if row["component"] in ("history", "shared"):
            continue
        key = tuple(row[field] for field in FIELDS)
        if row["release"] == SUPPLEMENT:
            key = ("", SUPPLEMENT) + key[2:]
        rows[key] = row
    return rows


def compare(found, records):
    """Return (missing, hook_differences, unused) between the renders and the reviewed records."""
    reviewed = reviewed_rows(records)
    missing = sorted(key for key in found if key not in reviewed)
    hooks = sorted(key for key in found if key in reviewed
                   and events(found[key]["hook"]) != events(reviewed[key].get("hookEvents", "")))
    unused = sorted(key for key in reviewed if key not in found)
    return missing, hooks, unused


def describe(key):
    component, release, scope, group, resource, name = key
    owner = "supplement" if release == SUPPLEMENT else f"{component} / {release}"
    return f"{owner}: {group or 'core'}/{resource} {scope}/{name}"


def verbs_for(key, hook):
    return HOOK_VERBS if hook else STEADY_VERBS


def proposal(key, info, supplement_component=""):
    """A record in the inventory's field order. A person reviews it before granting."""
    row = dict(zip(FIELDS, key))
    if key[1] == SUPPLEMENT:
        row["component"] = supplement_component
    row["verbs"] = verbs_for(key, info["hook"])
    if info["hook"]:
        row.update(source="pinned Helm upgrade render", hookEvents=info["hook"])
    elif key[1] == SUPPLEMENT:
        row["source"] = "kubectl apply"
    else:
        row["source"] = "reviewed Jaeger render" if key[1] == "jaeger" else "pinned Helm upgrade render"
    return row


def supplement_component(key, records):
    """The component that applies supplemental objects in a namespace, when the inventory names one."""
    scope = key[2]
    candidates = {row["component"] for row in records if row["release"] == SUPPLEMENT and row["scope"] == scope}
    if len(candidates) == 1:
        return candidates.pop()
    return {"default": "oci-jaeger", "jaeger": "otel-jaeger"}.get(scope, "otel-jaeger")


def gather(root, records, version):
    """Render the six releases the way the scripts install them. Returns {identity: {kind, hook}}."""
    found = {}
    reviewed = reviewed_rows(records)
    with tempfile.TemporaryDirectory(prefix="chart-objects-") as scratch:
        # An upgrade render without CRDs: Helm neither upgrades nor deletes crds/ objects.
        render_diff.render(root, Path(scratch) / "render", True,
                           target=["--kube-version", version, "--is-upgrade"], index=collector(found, reviewed))
    return found


def report(found, records, out):
    missing, hooks, unused = compare(found, records)
    print(f"Compared {len(found)} rendered objects with {len(reviewed_rows(records))} reviewed records.", file=out)
    if unused:
        print(f"\n{len(unused)} reviewed record(s) match no rendered object. Keep them until every release has been\n"
              "upgraded past the chart that rendered them and no rollback target needs them:", file=out)
        for key in unused:
            print("  - " + describe(key), file=out)
    if hooks:
        print(f"\n{len(hooks)} object(s) have Helm hook events that differ from their record:", file=out)
        for key in hooks:
            print(f"  - {describe(key)}: rendered '{found[key]['hook']}'", file=out)
    if missing:
        print(f"\n{len(missing)} rendered object(s) have no reviewed record:", file=out)
        for key in missing:
            protected = " (PROTECTED kind: grants or carries privilege)" if found[key]["kind"] in PROTECTED else ""
            print(f"  - {describe(key)}{protected}", file=out)
    return missing, hooks


def rewrite(document, found):
    """Add the missing records and correct hook events. Returns the number of changes."""
    records = document["records"]
    missing, hooks, _ = compare(found, records)
    reviewed = reviewed_rows(records)
    for key in hooks:
        row = reviewed[key]
        row.pop("hookEvents", None)
        row["verbs"] = verbs_for(key, found[key]["hook"])
        if found[key]["hook"]:
            row["hookEvents"] = found[key]["hook"]
    for key in missing:
        row = proposal(key, found[key], supplement_component(key, records))
        same = [i for i, old in enumerate(records) if old["component"] == row["component"] and old["release"] == row["release"]]
        tail = [i for i, old in enumerate(records) if old["component"] in ("history", "shared")]
        records.insert(same[-1] + 1 if same else tail[0] if tail else len(records), row)
    return len(missing) + len(hooks)


def main(argv=None, out=sys.stdout, root=ROOT):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="exit 1 when the inventory lacks a rendered object")
    mode.add_argument("--write", action="store_true", help="add the missing records and correct hook events")
    args = parser.parse_args(argv)
    path = root / INVENTORY.relative_to(ROOT)
    document = json.loads(path.read_text())
    try:
        found = gather(root, document["records"], kube_version(root / TOOLS.relative_to(ROOT)))
    except (KeyError, TypeError, ValueError, OSError) as error:
        print("error: " + str(error), file=sys.stderr)
        return 2
    missing, hooks = report(found, document["records"], out)
    if not (missing or hooks):
        print("\nEvery rendered object has a reviewed record.", file=out)
        return 0
    if args.write:
        count = rewrite(document, found)
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        print(f"\nWrote {count} change(s) to {path.relative_to(root)}. Review each record and the PROTECTED kinds, then run\n"
              "scripts/demo/deployer_access_test.py: it fails when ops/rbac/deployer.yaml does not cover the new\n"
              "objects. A change to that role is an access change that an administrator applies before the\n"
              "next deployment: " + PROCEDURE, file=out)
        return 0
    print("\nThe pinned charts and " + str(path.relative_to(root)) + " differ. Run\n"
          "  python3 scripts/demo/chart_objects.py --write\n"
          "and review each record. Then run scripts/demo/deployer_access_test.py: it fails when\n"
          "ops/rbac/deployer.yaml does not cover the new objects. A change to that role is an\n"
          "access change that an administrator applies before the next deployment: " + PROCEDURE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
