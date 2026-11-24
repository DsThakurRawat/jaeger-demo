#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Exercise the companion chart on a newly created, disposable local API server."""

import argparse
import base64
import gzip
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile

import yaml
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import companion_ownership
import deployer_access
import release_phases
from companion_values import ROOT, source_values

# A rehearsal identity bound to the committed deployer roles. It matches the OCI user OCID pattern.
DEPLOYER_USER = "ocid1.user.oc1..aaaarehearsalcompanion"


def exercise(cfg, context):
    # The cluster admin (kubectl, kube) builds the fixture: namespaces, seeded objects, the role and
    # its bindings. It also applies the reviewed ownership patches, which are an operator step, and
    # inspects and manipulates release records and objects that a check compares. The first install,
    # the Helmfile applies and the chart upgrade run as the bound deployer user (see `deployer`).
    kubectl = ["kubectl", "--kubeconfig", str(cfg), "--context", context]

    def kube(args, body=None):
        return subprocess.check_output(kubectl + args, input=body, text=True).strip()

    view = json.loads(kube(["config", "view", "--minify", "-o", "json"]))
    if urlsplit(view["clusters"][0]["cluster"]["server"]).hostname != "127.0.0.1":
        raise ValueError("This rehearsal requires a disposable loopback API server")
    namespaces = set(kube(["get", "ns", "-o", "jsonpath={.items[*].metadata.name}"]).split())
    bootstrap = {"default", "kube-public", "kube-system", "kube-node-lease"}
    if context == "kind-jaeger-demo-adoption-fixture":
        bootstrap.add("local-path-storage")
    if namespaces - bootstrap or not {"default", "kube-system"}.issubset(namespaces):
        raise ValueError("Use a new fixture with only Kubernetes bootstrap namespaces")
    existing = set(kube(["get", "deploy,svc,ing,cm", "-n", "default", "-o", "name"]).splitlines())
    if existing - {"service/kubernetes", "configmap/kube-root-ca.crt"} or "service/kubernetes" not in existing:
        raise ValueError("The default namespace must contain only Kubernetes bootstrap resources")
    values = source_values()
    for ns, inputs in values["companions"].items():
        if ns != "default":
            kube(["create", "namespace", ns])
        for obj in inputs["objects"]:
            kube(["-n", ns, "apply", "-f", "-"], json.dumps(obj))
    # The committed deployer roles, bound to a rehearsal user. They need all four namespaces.
    kube(["apply", "-f", str(deployer_access.ROLES)])
    kube(["apply", "-f", "-"], json.dumps(deployer_access.bindings(DEPLOYER_USER)))
    # The old scripts restart these deployments after applying their manifests.
    for ns in ("default", "jaeger"):
        kube(["-n", ns, "rollout", "restart", "deployment/trace-generator"])

    if context == "kind-jaeger-demo-adoption-fixture":
        for ns in ("default", "jaeger"):
            kube(["-n", ns, "rollout", "status", "deployment/trace-generator", "--timeout=5m"])

    # A disagreement on any one object stops planning the whole adoption.
    target = ["-n", "default", "annotate", "service/prometheus"]
    kube(target + ["meta.helm.sh/release-name=another-fixture-release"])
    try:
        companion_ownership.plan(str(cfg), context)
    except ValueError as exc:
        assert "Conflicting" in str(exc)
    else:
        raise AssertionError("Planner accepted a foreign-owned Service")
    kube(target + ["meta.helm.sh/release-name-"])

    # The fixture reader receives only named GET access, matching the planner.
    allowed = yaml.safe_load(companion_ownership.ALLOWLIST.read_text())
    reader = "fixture-companion-reader"
    groups = {"ConfigMap": ("", "configmaps"), "Service": ("", "services"),
              "Deployment": ("apps", "deployments"), "Ingress": ("networking.k8s.io", "ingresses")}
    for ns, kinds in allowed.items():
        rules = [{"apiGroups": [groups[k][0]], "resources": [groups[k][1]], "verbs": ["get"],
                  "resourceNames": names} for k, names in kinds.items()]
        rbac = {"apiVersion": "rbac.authorization.k8s.io/v1", "metadata": {"namespace": ns, "name": reader}}
        kube(["create", "-f", "-"], json.dumps(dict(rbac, kind="Role", rules=rules)))
        kube(["create", "-f", "-"], json.dumps(dict(rbac, kind="RoleBinding",
             roleRef={"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": reader},
             subjects=[{"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": reader}])))
    with tempfile.TemporaryDirectory() as temp:
        config = json.loads(kube(["config", "view", "--flatten", "--minify", "--raw", "-o", "json"]))
        config["users"][0]["user"].update({"as": reader, "as-groups": ["system:authenticated"]})
        path = Path(temp) / "reader.kubeconfig"
        with open(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as f:
            json.dump(config, f)
        desired = companion_ownership.plan(str(path), context)
        for verb, resource in (("patch", "service/prometheus"), ("get", "secrets")):
            result = subprocess.run(["kubectl", "--kubeconfig", str(path), "--context", context, "-n", "default",
                                     "auth", "can-i", verb, resource], capture_output=True, text=True)
            assert result.returncode == 1 and result.stdout.strip() == "no"

    def get(item):
        return json.loads(kube(["-n", item["namespace"], "get", item["kind"], item["name"], "-o", "json"]))

    def states():
        return {(i["namespace"], i["kind"], i["name"]): (
            obj["metadata"]["uid"], obj.get("spec"), obj.get("data")
        ) for i in desired for obj in [get(i)]}

    def full_states():
        # Identity, payload and every label and annotation, so a metadata-only write is visible too.
        return {(i["namespace"], i["kind"], i["name"]): {
            "uid": obj["metadata"]["uid"], "spec": obj.get("spec"), "data": obj.get("data"),
            "labels": obj["metadata"].get("labels", {}), "annotations": obj["metadata"].get("annotations", {}),
        } for i in desired for obj in [get(i)]}

    before = states()
    original = full_states()
    assert all(resource["patch"] for resource in desired)
    # Apply the reviewed plan with the documented script. It writes the record for de-adoption.
    scratch = tempfile.TemporaryDirectory()
    work = Path(scratch.name)
    # The same cluster as the deployer user: the admin credentials with every request impersonating it.
    deployer_config = yaml.safe_load(Path(cfg).read_text())
    deployer_config["current-context"] = context
    for entry in deployer_config["users"]:
        entry["user"]["as"] = DEPLOYER_USER
        entry["user"]["as-groups"] = ["system:authenticated"]
    deployer = work / "deployer.kubeconfig"
    with open(os.open(deployer, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as f:
        yaml.safe_dump(deployer_config, f)
    who = subprocess.run(["kubectl", "--kubeconfig", str(deployer), "--context", context, "auth", "whoami",
                          "-o", "jsonpath={.status.userInfo.username}"], capture_output=True, text=True, check=True)
    assert who.stdout.strip() == DEPLOYER_USER, who.stdout
    planned, recorded = work / "ownership.json", work / "adopted.json"
    planned.write_text(json.dumps(desired))
    apply_script = str(ROOT / "scripts" / "demo" / "apply-ownership-patches.sh")

    def identities(items):
        return [(i["namespace"], i["kind"], i["name"]) for i in items]

    # One object changes after planning. Its patch must fail without a write and stop the run.
    stale = ("opensearch", "Ingress", "opensearch-demo-ingress")
    position = identities(desired).index(stale)
    assert 0 < position < len(desired) - 1
    touch = ["-n", stale[0], "annotate", "ingress/" + stale[2]]
    kube(touch + ["rehearsal.example/stale=1"])
    kube(touch + ["rehearsal.example/stale-"])
    partial = subprocess.run(["bash", apply_script, str(planned), str(cfg), context, str(recorded)],
                             capture_output=True, text=True)
    assert partial.returncode != 0, "A stale patch must stop the run"
    assert json.loads(recorded.read_text()) == desired[:position], "The record must hold exactly the applied patches"
    # Retry with a fresh plan and the same record. Adopted objects need no patch and keep their entries.
    retry = companion_ownership.plan(str(cfg), context)
    assert [bool(i["patch"]) for i in retry] == [False] * position + [True] * (len(desired) - position)
    (work / "ownership-retry.json").write_text(json.dumps(retry))
    subprocess.run(["bash", apply_script, str(work / "ownership-retry.json"), str(cfg), context, str(recorded)],
                   check=True, stdout=subprocess.DEVNULL)
    record = json.loads(recorded.read_text())
    assert identities(record) == identities(desired) and all(i["patch"] for i in record)
    assert record[:position] == desired[:position] and record[position:] == retry[position:]
    assert not any(i["patch"] for i in companion_ownership.plan(str(cfg), context))
    assert before == states()

    # The first companion install, as the deploy workflow runs it after the reviewed ownership patches:
    # a plain helm install, not atomic and without --take-ownership. Helm's upgrade --install cannot
    # require an absent release, whereas install fails on any stored record of that name and refuses
    # an existing object that lacks this release's ownership metadata.
    name = companion_ownership.RELEASE
    helm = ["helm", "--kubeconfig", str(deployer), "--kube-context", context]
    state_values = work / "state.json"
    state_values.write_text(json.dumps(values))
    refusals = 0

    def unchanged(expected, step):
        actual = full_states()
        # Name the changed fields and metadata keys only. Values can be long and are in the fixture anyway.
        changed = []
        for key, fields in expected.items():
            for field, value in fields.items():
                other = actual[key][field]
                if value != other and field in ("labels", "annotations"):
                    changed.append((key, field, sorted(k for k in value.keys() | other.keys() if value.get(k) != other.get(k))))
                elif value != other:
                    changed.append((key, field))
        assert not changed, f"{step} changed {changed}"

    def stored(ns):
        # Helm's own storage index: every revision and its status, including failed or pending ones.
        records = json.loads(kube(["-n", ns, "get", "secrets", "-l", "owner=helm,name=" + name, "-o", "json"]))["items"]
        return sorted((int(r["metadata"]["labels"]["version"]), r["metadata"]["labels"]["status"]) for r in records)

    subprocess.run(["helm", "package", str(ROOT / "charts" / "demo-resources"), "--destination", str(work)],
                   check=True, stdout=subprocess.DEVNULL)
    archive, = work.glob("demo-resources-*.tgz")

    def install(ns):
        inputs = work / (ns + ".json")
        inputs.write_text(json.dumps({"objects": values["companions"][ns]["objects"]}))
        # The arguments the deploy driver uses, as the deployer user.
        return subprocess.run(helm + release_phases.install_args(ns, archive, inputs), capture_output=True, text=True)

    def apply(ns):
        # Native Helmfile on one existing companion release. For an absent release the diff fails
        # first (--allow-unreleased=false), and --install=false keeps Helmfile's upgrade --install
        # from installing one.
        return subprocess.run(["helmfile", "--kubeconfig", str(deployer), "--kube-context", context,
                               "-f", str(ROOT / "helmfile-resources.yaml.gotmpl"), "--state-values-file", str(state_values),
                               "-l", "namespace=" + ns, "--skip-deps", "apply", "--concurrency", "1", "--reset-values",
                               "--sync-args=--install=false --rollback-on-failure=false --create-namespace=false",
                               "--diff-args=--allow-unreleased=false", "--suppress-secrets"],
                              capture_output=True, text=True)

    def succeeded(result, step):
        assert result.returncode == 0, f"{step} failed: {result.stderr[-2000:]}"

    def refused(ns, step, action, expected):
        snapshot = full_states()
        result = action(ns)
        assert result.returncode, f"{step} in {ns} was not refused"
        assert stored(ns) == expected, f"{step} changed the stored release in {ns}"
        unchanged(snapshot, f"{step} in {ns}")
        return 1

    def first_install(ns):
        """Refuse each unsafe variant without a write, then install and prove a no-op. Requires an absent release."""
        objects = values["companions"][ns]["objects"]
        assert stored(ns) == []
        # The upgrade path never installs an absent release.
        count = refused(ns, "Apply of the absent release", apply, [])
        # Without --take-ownership, Helm never adopts an object that lacks this release's ownership metadata.
        first = ["-n", ns, "annotate", objects[0]["kind"] + "/" + objects[0]["metadata"]["name"]]
        kube(first + ["meta.helm.sh/release-name-"])
        count += refused(ns, "Install over an object without release metadata", install, [])
        kube(first + ["meta.helm.sh/release-name=" + name])
        snapshot = full_states()
        succeeded(install(ns), f"First install in {ns}")
        assert stored(ns) == [(1, "deployed")], f"First install did not store one deployed revision in {ns}"
        unchanged(snapshot, f"First install in {ns}")
        # A stored release cannot be installed again. Helmfile apply is then a no-op.
        count += refused(ns, "Second install", install, [(1, "deployed")])
        succeeded(apply(ns), f"Apply after the first install in {ns}")
        assert stored(ns) == [(1, "deployed")], f"No-op apply added a revision in {ns}"
        unchanged(snapshot, f"No-op apply in {ns}")
        return count

    # First contact. As in production, Helm has never applied these objects: kubectl created them
    # and the ownership patches are the only later write. Envtest runs no workload controllers,
    # so only the kind node can wait for the generator Deployment in the jaeger namespace.
    first_contact = ["otel-demo"] + (["jaeger"] if context == "kind-jaeger-demo-adoption-fixture" else [])
    for ns in first_contact:
        refusals += first_install(ns)
    assert before == states()

    # The direct apply below checks the chart and the state file from the working tree. For the
    # namespaces installed above it must find nothing to change.
    with tempfile.TemporaryDirectory() as tmp:
        inputs = Path(tmp) / "state.json"
        inputs.write_text(json.dumps(values))
        cmd = ["helmfile", "--kubeconfig", str(deployer), "--kube-context", context, "-f",
               str(ROOT / "helmfile-resources.yaml.gotmpl"), "--state-values-file", str(inputs)]
        subprocess.run(cmd + ["apply", "--skip-deps", "--concurrency", "1"], check=True)
        after = states()
        changed = [key for key in before if before[key] != after[key]]
        if changed:
            raise AssertionError(f"Adoption changed identity, spec or data for {changed}")
        def history(ns):
            return json.loads(subprocess.check_output([
                "helm", "--kubeconfig", str(deployer), "--kube-context", context, "-n", ns,
                "history", "demo-resources", "--output", "json"], text=True))
        revisions = {ns: history(ns) for ns in values["companions"]}
        assert all([r["status"] for r in rows] == ["deployed"] for rows in revisions.values())
        subprocess.run(cmd + ["apply", "--skip-deps", "--concurrency", "1"], check=True)
        assert revisions == {ns: history(ns) for ns in values["companions"]}
        assert before == states()

        # Simulate a later removal from the chart and the reviewed allowlist.
        chart = Path(tmp) / "chart"
        shutil.copytree(ROOT / "charts" / "demo-resources", chart)
        retained = ("default", "Service", "prometheus")
        allowed["default"]["Service"].remove("prometheus")
        (chart / "allowlist.yaml").write_text(yaml.safe_dump(allowed))
        remaining = [o for o in values["companions"]["default"]["objects"]
                     if (o["kind"], o["metadata"]["name"]) != retained[1:]]
        subprocess.run(["helm", "--kubeconfig", str(deployer), "--kube-context", context, "-n", "default",
                        "upgrade", "demo-resources", str(chart), "--reset-values", "-f", "-"],
                       input=json.dumps({"objects": remaining}), text=True, check=True)
        assert states()[retained] == before[retained]

    # Recovery. A first install that fails after Helm stored its record leaves failed revision 1.
    # Construct that record, then de-adopt as docs/cutover.md requires: remove only the stored
    # release record. helm uninstall is never used on a companion release.
    ns = "opensearch"
    assert stored(ns) == [(1, "deployed")]
    secret = f"sh.helm.release.v1.{name}.v1"
    record = json.loads(kube(["-n", ns, "get", "secret", secret, "-o", "json"]))
    payload = json.loads(gzip.decompress(base64.b64decode(base64.b64decode(record["data"]["release"]))))
    assert (payload["name"], payload["namespace"], payload["version"]) == (name, ns, 1)
    payload["info"]["status"] = "failed"
    record["data"]["release"] = base64.b64encode(base64.b64encode(gzip.compress(json.dumps(payload).encode()))).decode()
    record["metadata"]["labels"]["status"] = "failed"
    kube(["-n", ns, "replace", "-f", "-"], json.dumps(record))
    refusals += refused(ns, "Install over failed revision 1", install, [(1, "failed")])
    snapshot = full_states()
    selector = "owner=helm,name=" + name
    kube(["-n", ns, "delete", "secret", "-l", selector])
    assert stored(ns) == []
    unchanged(snapshot, f"Removing the stored release record in {ns}")
    refusals += first_install(ns)

    # Companion rollback with the commands in docs/cutover.md: remove only the stored release
    # records, then only the recorded ownership metadata. The objects return to their state
    # before adoption and the source manifests apply with no difference.
    for ns in first_contact:
        kube(["-n", ns, "delete", "secret", "-l", selector])
        assert stored(ns) == []
        release = work / (ns + "-de-adoption.json")
        release.write_text(json.dumps(companion_ownership.release_plan(str(cfg), context, recorded, ns)))
        subprocess.run(["bash", apply_script, str(release), str(cfg), context], check=True, stdout=subprocess.DEVNULL)
        assert not any(i["patch"] for i in companion_ownership.release_plan(str(cfg), context, recorded, ns))
        def additions(items):
            return {(i["kind"], i["name"]): [op for op in i["patch"] if op["path"] != "/metadata/resourceVersion"]
                    for i in items if i["namespace"] == ns}
        assert additions(companion_ownership.plan(str(cfg), context, ns)) == additions(desired), f"{ns} is not ready for a fresh adoption"
        for obj in values["companions"][ns]["objects"]:
            diff = subprocess.run(kubectl + ["-n", ns, "diff", "-f", "-"], input=json.dumps(obj), text=True, capture_output=True)
            assert diff.returncode == 0, f"De-adopted {ns}/{obj['kind']}/{obj['metadata']['name']} differs from its source"
    unchanged({key: fields for key, fields in original.items() if key[0] in first_contact}, "Adoption followed by de-adoption")
    scratch.cleanup()
    print(json.dumps({"objectsPreserved": len(before), "releases": len(revisions),
                      "secondApplyCreatedRevisions": 0, "deploymentRestartAnnotationsPreserved": True,
                      "readOnlyPlan": True, "foreignOwnerRejected": True, "removedServiceKept": True,
                      "firstInstallNamespaces": len(first_contact), "refusalsWithoutChange": refusals,
                      "firstInstallNoOpCreatedRevisions": 0, "failedFirstInstallRecovered": True,
                      "recordRemovalKeptObjects": True, "partialAdoptionRetriedWithOneRecord": True,
                      "deAdoptedNamespaces": len(first_contact),
                      "deAdoptionRestoredMetadata": True, "helmRanAsDeployer": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True, choices=("fixture", "kind-jaeger-demo-adoption-fixture"))
    args = parser.parse_args()
    if not __debug__:
        parser.error("Rehearsal assertions must be enabled")
    exercise(args.kubeconfig, args.context)
