#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Exercise the companion chart on a newly created, disposable local API server."""

import argparse
import base64
import copy
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
from adoption_bundle import BundleError
from companion_values import ROOT, source_values
from helmfile_transaction import reconcile


def exercise(cfg, context):
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

    before = states()
    for resource in desired:
        assert resource["patch"]
        kube(["-n", resource["namespace"], "patch", resource["kind"], resource["name"], "--type=json",
              "--patch", json.dumps(resource["patch"])])
    assert not any(i["patch"] for i in companion_ownership.plan(str(cfg), context))
    assert before == states()

    # Frozen companion transaction, as production runs it from an approved bundle.
    name = companion_ownership.RELEASE
    refusals = 0

    def full_states():
        # Identity, payload and every label and annotation, so a metadata-only write is visible too.
        return {(i["namespace"], i["kind"], i["name"]): {
            "uid": obj["metadata"]["uid"], "spec": obj.get("spec"), "data": obj.get("data"),
            "labels": obj["metadata"].get("labels", {}), "annotations": obj["metadata"].get("annotations", {}),
        } for i in desired for obj in [get(i)]}

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

    def row(ns, operation):
        return {"id": ns + "/" + name, "helm": {"operation": operation, "atomic": False, "wait": "legacy",
                "timeoutSeconds": 300, "historyMax": 10, "skipCRDs": False, "createNamespace": False}}

    frozen_work = tempfile.TemporaryDirectory()
    work = Path(frozen_work.name)
    subprocess.run(["helm", "package", str(ROOT / "charts" / "demo-resources"), "--destination", str(work)],
                   check=True, stdout=subprocess.DEVNULL)
    archive, = work.glob("demo-resources-*.tgz")

    def frozen(ns, objects):
        inputs = work / (ns + ".json")
        inputs.write_text(json.dumps({"objects": objects}))
        render = subprocess.check_output(["helm", "template", name, str(archive), "--namespace", ns,
                                          "--values", str(inputs)], env=dict(os.environ, KUBECONFIG=os.devnull))
        return {"chart.tgz": archive.read_bytes(), "values.yaml": inputs.read_bytes(), "postrender.yaml": render}

    def refused(ns, operation, files, expected):
        snapshot = full_states()
        try:
            reconcile(row(ns, operation), "apply", files, kubeconfig=cfg, context=context)
        except BundleError:
            assert stored(ns) == expected, f"Refused {operation} changed the stored release in {ns}"
            unchanged(snapshot, f"Refused {operation} in {ns}")
            return 1
        raise AssertionError(f"Unexpected frozen {operation} in {ns}")

    def frozen_install(ns):
        """Refuse every unsafe variant without a write, then install and prove a no-op. Requires an absent release."""
        objects = values["companions"][ns]["objects"]
        approved = frozen(ns, objects)
        assert stored(ns) == []
        # No install fallback for an absent release, and no write for unapproved values.
        count = refused(ns, "upgrade", approved, [])
        changed = copy.deepcopy(objects)
        changed[0]["metadata"].setdefault("labels", {})["unapproved"] = "true"
        count += refused(ns, "install", dict(approved, **{"values.yaml": json.dumps({"objects": changed}).encode()}), [])
        # The callback never takes ownership of an object that lacks this release's metadata.
        first = ["-n", ns, "annotate", objects[0]["kind"] + "/" + objects[0]["metadata"]["name"]]
        kube(first + ["meta.helm.sh/release-name-"])
        count += refused(ns, "install", approved, [])
        kube(first + ["meta.helm.sh/release-name=" + name])
        snapshot = full_states()
        reconcile(row(ns, "install"), "apply", approved, kubeconfig=cfg, context=context)
        assert stored(ns) == [(1, "deployed")], f"Frozen install did not store one deployed revision in {ns}"
        unchanged(snapshot, f"Frozen install in {ns}")
        # A stored release cannot be installed again. The frozen upgrade path is then a no-op.
        count += refused(ns, "install", approved, [(1, "deployed")])
        reconcile(row(ns, "upgrade"), "apply", approved, kubeconfig=cfg, context=context)
        assert stored(ns) == [(1, "deployed")], f"Frozen no-op added a revision in {ns}"
        unchanged(snapshot, f"Frozen no-op in {ns}")
        return count

    # First contact. As in production, Helm has never applied these objects: kubectl created them
    # and the ownership patches are the only later write. Envtest runs no workload controllers,
    # so only the kind node can wait for the generator Deployment in the jaeger namespace.
    first_contact = ["otel-demo"] + (["jaeger"] if context == "kind-jaeger-demo-adoption-fixture" else [])
    for ns in first_contact:
        refusals += frozen_install(ns)
    assert before == states()

    # The direct apply below checks the chart and the state file from the working tree. For the
    # namespaces installed above it must find nothing to change.
    with tempfile.TemporaryDirectory() as tmp:
        inputs = Path(tmp) / "state.json"
        inputs.write_text(json.dumps(values))
        cmd = ["helmfile", "--kubeconfig", str(cfg), "--kube-context", context, "-f",
               str(ROOT / "helmfile-resources.yaml.gotmpl"), "--state-values-file", str(inputs)]
        subprocess.run(cmd + ["apply", "--skip-deps", "--concurrency", "1"], check=True)
        after = states()
        changed = [key for key in before if before[key] != after[key]]
        if changed:
            raise AssertionError(f"Adoption changed identity, spec or data for {changed}")
        def history(ns):
            return json.loads(subprocess.check_output([
                "helm", "--kubeconfig", str(cfg), "--kube-context", context, "-n", ns,
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
        subprocess.run(["helm", "--kubeconfig", str(cfg), "--kube-context", context, "-n", "default",
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
    approved = frozen(ns, values["companions"][ns]["objects"])
    refusals += refused(ns, "install", approved, [(1, "failed")])
    snapshot = full_states()
    kube(["-n", ns, "delete", "secret", secret])
    assert stored(ns) == []
    unchanged(snapshot, f"Removing the stored release record in {ns}")
    refusals += frozen_install(ns)
    frozen_work.cleanup()
    print(json.dumps({"objectsPreserved": len(before), "releases": len(revisions),
                      "secondApplyCreatedRevisions": 0, "deploymentRestartAnnotationsPreserved": True,
                      "readOnlyPlan": True, "foreignOwnerRejected": True, "removedServiceKept": True,
                      "frozenFirstContactNamespaces": len(first_contact), "frozenRefusalsWithoutChange": refusals,
                      "frozenNoOpCreatedRevisions": 0, "failedFirstInstallRecovered": True,
                      "recordRemovalKeptObjects": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True, choices=("fixture", "kind-jaeger-demo-adoption-fixture"))
    args = parser.parse_args()
    if not __debug__:
        parser.error("Rehearsal assertions must be enabled")
    exercise(args.kubeconfig, args.context)
