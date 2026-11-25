#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Keep ops/rbac/chart-objects.json equal to what the script and Helmfile paths render.

scripts/demo/deployer_access.py derives the deployer's Kubernetes access from the
object inventory in ops/rbac/chart-objects.json. A chart bump that renders a new
object would otherwise fail at deployment with a Forbidden error, after the
change had merged. `--check` renders, without cluster access, the six releases
the way the deployment scripts install them and the ten Helmfile releases (the
six applications from the lock-verified .helmfile-locked.yaml and the four
demo-resources companions). Both paths must render the same object identities
and hook events; the companions render the supplemental objects the scripts
apply. Each Helmfile release must have Helm history records, the Secrets Helm
keeps its revisions in. `--check` exits non-zero when an object or a release
has no record, a record's Helm hook events differ, or the inventory's chart
archive list differs from scripts/demo/chart-archives.lock.json. `--write` adds
the missing records, corrects the hook events and copies the archive list.

Only object identities are printed: kind, API group, namespace and name. A
client-side render has no live discovery and no Helm lookup results, so this
compares names, kinds and scope, not manifests. A record that no pinned chart
renders any more is reported and never removed: Helm deletes the object on the
next upgrade, which needs the access, and a rollback target may still hold it.
Remove such a record by hand once no release needs it.

`--check` and `--write` also render the Helmfile releases with `--include-crds`
and fail when the CRD names differ from the named CRD `get` grant in
ops/rbac/deployer.yaml, which scripts/demo/live_drift.py reads them with.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import chart_archives
import deployer_access
import pr_render_diff as render_diff


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "ops/rbac/chart-objects.json"
TOOLS = Path(__file__).with_name("helmfile-tools.lock.json")
# Where make validate (equivalence.py) also pulls the lock-verified archives.
ARCHIVES = ".tools/chart-archives"
PROCEDURE = "docs/deployer-access.md#changing-the-role"
FIELDS = ("component", "release", "scope", "apiGroup", "resource", "name")
# The release each Helm release name and namespace belongs to in the inventory.
COMPONENTS = {
    ("default", "prometheus"): "oci-monitoring", ("default", "jaeger"): "oci-jaeger",
    ("opensearch", "opensearch"): "opensearch", ("opensearch", "opensearch-dashboards"): "opensearch",
    ("jaeger", "jaeger"): "otel-jaeger", ("otel-demo", "otel-demo"): "otel-demo",
}
SUPPLEMENT = "supplement"
# Helm keeps each revision of a release as the Secret sh.helm.release.v1.<release>.v<N> in its
# namespace. Records of one release share one identity: the access is for the kind, whatever N.
HISTORY = "history"
RELEASE_RECORD = "sh.helm.release.v1."
HISTORY_VERBS = ["get", "create", "update", "delete"]
CHECKPOINT = "shared"
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


def history_key(namespace, release):
    """The identity of one Helm release's records: Secrets in its namespace, whatever the revision."""
    return (HISTORY, release, namespace, "", "secrets", "")


def reviewed_rows(records):
    """Records for chart, supplemental and Helm history objects. The checkpoint is not rendered.

    The history records of one release, one per example revision, map to its history_key.
    """
    rows = {}
    for row in records:
        if row["component"] == CHECKPOINT:
            continue
        if row["component"] == HISTORY:
            prefix = RELEASE_RECORD + row["release"] + ".v"
            if (row["apiGroup"], row["resource"]) != ("", "secrets") or not row["name"].startswith(prefix) \
                    or not row["name"][len(prefix):].isdigit():
                raise ValueError(f"History record {row['scope']}/{row['name']} is not a Helm release record of {row['release']}")
            rows.setdefault(history_key(row["scope"], row["release"]), row)
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
    if component == HISTORY:
        return f"history: Helm release records of {scope}/{release} ({RELEASE_RECORD}{release}.v<N>)"
    owner = "supplement" if release == SUPPLEMENT else f"{component} / {release}"
    return f"{owner}: {group or 'core'}/{resource} {scope}/{name}"


def verbs_for(key, hook):
    return HOOK_VERBS if hook else STEADY_VERBS


def proposal(key, info, supplement_component=""):
    """A record in the inventory's field order. A person reviews it before granting."""
    row = dict(zip(FIELDS, key))
    if key[0] == HISTORY:
        # The first revision as the example. deployer_access.py asks for the kind, whatever N.
        row.update(name=f"{RELEASE_RECORD}{key[1]}.v1", verbs=list(HISTORY_VERBS),
                   source="first install example; every revision needs the same access, and history pruning deletes older ones")
        return row
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


def locked_state(root):
    """Write .helmfile-locked.yaml from the lock-verified archives and return its path relative to root."""
    archives = Path(root) / ARCHIVES
    archives.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="chart-objects-helm-") as home:
        # Pull with an empty Helm configuration, as the renders do, so a local repository list cannot interfere.
        env = dict(os.environ, KUBECONFIG=os.devnull, HELM_CONFIG_HOME=os.path.join(home, "config"),
                   HELM_CACHE_HOME=os.path.join(home, "cache"), HELM_DATA_HOME=os.path.join(home, "data"))
        chart_archives.locked_state(root, archives, run=lambda args, **kwargs: subprocess.run(args, env=env, **kwargs))
    return chart_archives.LOCKED_STATE


def path_differences(scripts, helmfile):
    """Where the script and Helmfile renders disagree on an identity or its hook events, as text lines."""
    lines = [f"only the script path renders {describe(key)}" for key in sorted(scripts.keys() - helmfile.keys())]
    lines += [f"only the Helmfile path renders {describe(key)}" for key in sorted(helmfile.keys() - scripts.keys())]
    lines += [f"hook events differ for {describe(key)}: script '{scripts[key]['hook']}', "
              f"Helmfile '{helmfile[key]['hook']}'" for key in sorted(scripts.keys() & helmfile.keys())
              if events(scripts[key]["hook"]) != events(helmfile[key]["hook"])]
    return lines


def gather(root, records, version, state):
    """Render the script path and the Helmfile path. Returns {identity: {kind, hook}}.

    Both must render the same identities and hook events: the six application releases, and
    the supplemental objects that the scripts apply and the four demo-resources companions own.
    Each Helmfile release adds the identity of its Helm records (history_key). `state` is the
    lock-verified application Helmfile from locked_state(), rendered with the companions.
    """
    reviewed = reviewed_rows(records)
    scripts, helmfile, releases = {}, {}, set()
    helmfile_index = collector(helmfile, reviewed)

    def index(records_, namespace, release, docs):
        releases.add((namespace, release))
        helmfile_index(records_, namespace, release, docs)

    with tempfile.TemporaryDirectory(prefix="chart-objects-") as scratch:
        # Upgrade renders without CRDs: Helm neither upgrades nor deletes crds/ objects.
        render_diff.render(root, Path(scratch) / "scripts", True,
                           target=["--kube-version", version, "--is-upgrade"], index=collector(scripts, reviewed))
        # helmfile template has no --is-upgrade flag of its own; it passes this one to helm template.
        render_diff.render(root, Path(scratch) / "helmfile", False, state=state,
                           target=["--kube-version", version, "--template-args=--is-upgrade"], index=index)
    differences = path_differences(scripts, helmfile)
    if differences:
        raise ValueError("The script and Helmfile renders differ:\n  - " + "\n  - ".join(differences))
    for namespace, release in sorted(releases):
        helmfile[history_key(namespace, release)] = {"kind": "Secret", "hook": ""}
    return helmfile


def rendered_crds(root, version, state):
    """The CRD names the Helmfile releases render with --include-crds, which the drift report compares."""
    names = set()

    def index(_, namespace, release, docs):
        names.update(obj["metadata"]["name"] for obj in docs if obj and obj["kind"] == "CustomResourceDefinition")

    with tempfile.TemporaryDirectory(prefix="chart-crds-") as scratch:
        render_diff.render(root, Path(scratch) / "render", False, state=state,
                           target=["--kube-version", version, "--include-crds"], index=index)
    return names


def locked_archives(root):
    """The chart archive digests the inventory header must repeat: the chart lock's, never typed by hand."""
    return dict(sorted(chart_archives.load_lock(root)["charts"].items()))


def granted_crds(roles_path):
    """The CRD names the deployer role may read by name."""
    roles = deployer_access.load_roles(roles_path)
    return {name for (role, namespace), rules in roles.items() if role == deployer_access.DEPLOYER and not namespace
            for rule in rules if "customresourcedefinitions" in rule["resources"] for name in rule.get("resourceNames", [])}


def report_crds(rendered, granted, out):
    """Print the CRD names that the render and the grant do not share. Return whether they differ."""
    if rendered == granted:
        print(f"The {len(rendered)} rendered CRDs match the deployer role's named CRD reads.", file=out)
        return False
    for name in sorted(rendered - granted):
        print(f"  - CRD rendered but not granted: {name}", file=out)
    for name in sorted(granted - rendered):
        print(f"  - CRD granted but not rendered: {name}", file=out)
    return True


def report(found, records, out):
    missing, hooks, unused = compare(found, records)
    print(f"Compared {len(found)} rendered identities (objects and Helm release records) with "
          f"{len(reviewed_rows(records))} reviewed ones.", file=out)
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
        if row["component"] == HISTORY:
            # Helm history stays last.
            history = [i for i, old in enumerate(records) if old["component"] == HISTORY]
            records.insert(history[-1] + 1 if history else len(records), row)
            continue
        same = [i for i, old in enumerate(records) if old["component"] == row["component"] and old["release"] == row["release"]]
        tail = [i for i, old in enumerate(records) if old["component"] in (HISTORY, CHECKPOINT)]
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
        version = kube_version(root / TOOLS.relative_to(ROOT))
        archives = locked_archives(root)
        state = locked_state(root)
        found = gather(root, document["records"], version, state)
        crds = rendered_crds(root, version, state)
        granted = granted_crds(root / deployer_access.ROLES.relative_to(ROOT))
    except (KeyError, TypeError, ValueError, OSError) as error:
        print("error: " + str(error), file=sys.stderr)
        return 2
    missing, hooks = report(found, document["records"], out)
    stale = document.get("chartArchiveSHA256") != archives
    if stale:
        print(f"\nThe inventory's chartArchiveSHA256 differs from the charts of {chart_archives.LOCK}.", file=out)
    crd_drift = report_crds(crds, granted, out)
    if crd_drift:
        # --write cannot fix this: the grant is an access change a person reviews.
        print("\nThe rendered CRDs and the named CRD get grant in ops/rbac/deployer.yaml differ. Change the grant\n"
              "and MONITORING_CRDS in scripts/demo/deployer_access.py together: " + PROCEDURE, file=sys.stderr)
    if not (missing or hooks or stale):
        print("\nEvery rendered object and Helmfile release has a reviewed record.", file=out)
        return int(crd_drift)
    if args.write:
        count = rewrite(document, found)
        if stale:
            document["chartArchiveSHA256"] = archives
            count += 1
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        print(f"\nWrote {count} change(s) to {path.relative_to(root)}. Review each record and the PROTECTED kinds, then run\n"
              "scripts/demo/deployer_access_test.py: it fails when ops/rbac/deployer.yaml does not cover the new\n"
              "objects. A change to that role is an access change that an administrator applies before the\n"
              "next deployment: " + PROCEDURE, file=out)
        return int(crd_drift)
    print("\nThe pinned charts and " + str(path.relative_to(root)) + " differ. Run\n"
          "  python3 scripts/demo/chart_objects.py --write\n"
          "and review each record. Then run scripts/demo/deployer_access_test.py: it fails when\n"
          "ops/rbac/deployer.yaml does not cover the new objects. A change to that role is an\n"
          "access change that an administrator applies before the next deployment: " + PROCEDURE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
