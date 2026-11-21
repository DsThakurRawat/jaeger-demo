#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Exercise native Helmfile apply/sync against two synthetic releases on disposable Kubernetes."""

import argparse
import base64
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import release_phases


def exercise(cfg, context):
    ctl = ["kubectl", "--kubeconfig", str(cfg), "--context", context]
    helm = ["helm", "--kubeconfig", str(cfg), "--kube-context", context]
    view = json.loads(subprocess.check_output(ctl + ["config", "view", "--minify", "-o", "json"]))
    if urlsplit(view["clusters"][0]["cluster"]["server"]).hostname != "127.0.0.1":
        raise ValueError("A disposable loopback cluster is required")
    name = "jaeger-phase-fixture"
    namespaces = ["default", "jaeger-phase-fixture"]
    # Creation must fail on reuse. Leave cleanup to the disposable cluster owner.
    subprocess.run(ctl + ["create", "namespace", namespaces[1]], check=True)
    helm_rows = [{"name": name, "namespace": ns, "chart": "./chart", "labels": {"testns": ns}}
                 for ns in namespaces]
    rows = [{"id": ns + "/" + name, "observe": [], "rollback": {"revision": 1}} for ns in namespaces]
    plan = {"releases": rows, "images": {}}

    def latest(ns):
        return json.loads(subprocess.check_output(helm + ["-n", ns, "status", name, "-o", "json"]))

    def observe(scope):
        result = {"cluster": "fixture", "healthy": True, "capabilitiesSHA256": "fixture",
                  "objects": {}, "rollbackTargets": {}, "releases": {}}
        for rid in scope["releases"]:
            record = latest(rid.split("/")[0])
            checksum = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
            result["releases"][rid] = {"status": record["info"]["status"], "revision": record["version"],
                                       "recordSHA256": checksum}
            result["rollbackTargets"][rid] = {"revision": 1}
        return result

    def baseline():
        return observe(release_phases.observation_scope(rows, "fixture", {}))

    def force_test_status(ns, status):
        # Construct states left by interrupted transactions, including Helm's
        # storage index and its encoded status. Only the named fixture is used.
        revision = latest(ns)["version"]
        secret = f"sh.helm.release.v1.{name}.v{revision}"
        args = ctl + ["-n", ns]
        old = json.loads(subprocess.check_output(args + ["get", "secret", secret, "-o", "json"]))
        payload = json.loads(gzip.decompress(base64.b64decode(base64.b64decode(old["data"]["release"]))))
        assert (payload["name"], payload["namespace"], payload["version"]) == (name, ns, revision)
        payload["info"]["status"] = status
        old["data"]["release"] = base64.b64encode(base64.b64encode(gzip.compress(json.dumps(payload).encode()))).decode()
        old["metadata"]["labels"]["status"] = status
        subprocess.run(args + ["replace", "-f", "-"], input=json.dumps(old).encode(), stdout=subprocess.DEVNULL, check=True)

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        chart = root / "chart"
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text("apiVersion: v2\nname: phase-fixture\nversion: 1.0.0\n")
        (chart / "templates" / "config.yaml").write_text(
            'apiVersion: v1\nkind: ConfigMap\nmetadata: {name: jaeger-phase-fixture}\ndata: {stable: "yes"}\n')
        state = root / "helmfile.yaml"
        state.write_text(yaml.safe_dump({"releases": helm_rows}))
        cmd = ["helmfile", "--kubeconfig", str(cfg), "--kube-context", context, "-f", str(state)]
        for ns in namespaces:
            subprocess.run(helm + ["-n", ns, "install", name, str(chart)], check=True)
        ran = []
        def reconcile(row, method):
            ns = row["id"].split("/")[0]
            subprocess.run(cmd + ["-l", "testns=" + ns, method, "--skip-deps", "--concurrency", "1"], check=True)
            ran.append((method, ns))

        def healthy(stack):
            ns = namespaces[0] if stack == "oci" else namespaces[1]
            cm = json.loads(subprocess.check_output(ctl + ["-n", ns, "get", "configmap", name, "-o", "json"]))
            assert cm["data"] == {"stable": "yes"}
            ran.append(("verified", ns))
            return True

        def revisions():
            return [latest(ns)["version"] for ns in namespaces]

        force_test_status(namespaces[0], "failed")
        diff = subprocess.run(["helm", "diff", "upgrade", name, str(chart), "-n", namespaces[0],
                               "--kubeconfig", str(cfg), "--kube-context", context, "--detailed-exitcode"])
        assert diff.returncode == 0, "The failed release must have an empty diff"
        result = release_phases.run(plan, baseline(), observe, reconcile, healthy)
        assert result["verifiedStacks"] == ["oci", "otel"] and revisions() == [2, 1]
        assert latest(namespaces[0])["info"]["status"] == "deployed"
        ran.clear()
        release_phases.run(plan, baseline(), observe, reconcile, healthy)
        assert revisions() == [2, 1] and ran == [("apply", namespaces[0]), ("verified", namespaces[0]),
                                                 ("apply", namespaces[1]), ("verified", namespaces[1])]
        ran.clear()
        def unavailable(stack):
            assert stack == "oci"
            raise RuntimeError("fixture OCI health unavailable")
        try:
            release_phases.run(plan, baseline(), observe, reconcile, unavailable)
        except RuntimeError as exc:
            assert str(exc) == "fixture OCI health unavailable"
        else:
            raise AssertionError("Failed OCI health accepted")
        assert ran == [("apply", namespaces[0])] and revisions() == [2, 1]
        ran.clear()
        force_test_status(namespaces[1], "pending-upgrade")
        try:
            release_phases.run(plan, baseline(), observe, reconcile, healthy)
        except ValueError as exc:
            assert "operator review" in str(exc)
        else:
            raise AssertionError("Pending OTel state accepted")
        assert not ran and revisions() == [2, 1]
        print(json.dumps({"failedEmptyDiffRecovered": True, "noOpNewRevisions": 0,
                          "ociHealthFailureBlockedOTel": True, "pendingBlockedAllReleases": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", choices=("fixture", "kind-jaeger-demo-adoption-fixture"), required=True)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Enable fixture assertions")
    exercise(args.kubeconfig, args.context)
