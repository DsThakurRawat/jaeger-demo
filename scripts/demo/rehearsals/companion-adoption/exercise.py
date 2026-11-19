#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Exercise the companion chart on a newly created, disposable local API server."""

import argparse
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
from companion_values import ROOT, source_values


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
    print(json.dumps({"objectsPreserved": len(before), "releases": len(revisions),
                      "secondApplyCreatedRevisions": 0, "deploymentRestartAnnotationsPreserved": True,
                      "readOnlyPlan": True, "foreignOwnerRejected": True, "removedServiceKept": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True, choices=("fixture", "kind-jaeger-demo-adoption-fixture"))
    args = parser.parse_args()
    if not __debug__:
        parser.error("Rehearsal assertions must be enabled")
    exercise(args.kubeconfig, args.context)
