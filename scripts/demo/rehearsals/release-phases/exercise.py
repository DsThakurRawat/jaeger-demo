#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Exercise native Helmfile apply/sync against two synthetic releases on disposable Kubernetes."""

import argparse
import base64
import gzip
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import release_phases

# Helmfile resolves ref+ strings in its own values. The direct path passes values to Helm, which keeps them.
LITERAL = "ref+unused-provider://literal"


def direct_transactions(cfg, context, root, force_status):
    """Reconcile one existing release with native Helmfile and the direct deployment flags.

    The flags match the deploy workflow's contract: upgrade an existing release in place and
    never install it. Helmfile prepends --install to every Helm upgrade, so --install=false in
    the sync arguments restores that boundary. Values go to Helm, not Helmfile.
    """
    ctl = ["kubectl", "--kubeconfig", str(cfg), "--context", context]
    name = ns = "jaeger-direct-fixture"
    helm = ["helm", "--kubeconfig", str(cfg), "--kube-context", context, "-n", ns]
    history = 3
    subprocess.run(ctl + ["create", "namespace", ns], check=True, stdout=subprocess.DEVNULL)
    chart = root / "direct-chart"
    (chart / "templates").mkdir(parents=True)
    # A vendored dependency with an unreachable repository: --skip-deps must not fetch it.
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: direct-fixture\nversion: 1.0.0\ndependencies:\n"
        "  - {name: child, version: 1.0.0, repository: 'https://127.0.0.1:1/unreachable'}\n")
    (chart / "charts" / "child").mkdir(parents=True)
    (chart / "charts" / "child" / "Chart.yaml").write_text("apiVersion: v2\nname: child\nversion: 1.0.0\n")
    # Chart defaults differ from every approved value, so a diff without the values file is not empty.
    (chart / "values.yaml").write_text("{value: default, sensitive: default, literal: default}\n")
    (chart / "templates" / "config.yaml").write_text(
        "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: jaeger-direct-fixture}\n"
        "data: {value: {{ .Values.value | quote }}, literal: {{ .Values.literal | quote }}}\n")
    (chart / "templates" / "secret.yaml").write_text(
        "apiVersion: v1\nkind: Secret\nmetadata: {name: jaeger-direct-fixture}\n"
        "data: {message: {{ .Values.sensitive | b64enc | quote }}}\n")
    subprocess.run(["helm", "package", str(chart), "--destination", str(root)], check=True, stdout=subprocess.DEVNULL)
    archive = root / "direct-fixture-1.0.0.tgz"
    state = {"helmDefaults": {"historyMax": history, "createNamespace": False, "timeout": 45},
             "releases": [{"name": name, "namespace": ns, "chart": "./" + archive.name}]}
    (root / "direct.yaml").write_text(yaml.safe_dump(state))
    values = root / "direct-values.yaml"

    def approve(value, sensitive):
        values.write_text(yaml.safe_dump({"value": value, "sensitive": sensitive, "literal": LITERAL}))

    def helmfile(method):
        sync = ["--install=false", "--values=./" + values.name, "--wait=legacy", "--rollback-on-failure=true",
                "--skip-crds=false", "--create-namespace=false"]
        command = ["helmfile", "--file", "direct.yaml", "--kubeconfig", str(cfg), "--kube-context", context,
                   "--skip-deps", method, "--concurrency", "1", "--reset-values", "--sync-args=" + " ".join(sync)]
        if method == "apply":
            command += ["--diff-args=--allow-unreleased=false --values=./" + values.name,
                        "--suppress-secrets", "--suppress-diff"]
        return subprocess.run(command, cwd=root, capture_output=True, text=True)

    def reconcile(method):
        result = helmfile(method)
        assert result.returncode == 0, f"Helmfile {method} failed: {result.stderr[-2000:]}"

    def refuse(method):
        result = helmfile(method)
        assert result.returncode, f"Helmfile {method} installed a missing release"

    def status():
        out = json.loads(subprocess.check_output(helm + ["status", name, "-o", "json"]))
        return out["version"], out["info"]["status"]

    def records():
        # Helm's storage index holds every retained revision, whatever its status.
        out = json.loads(subprocess.check_output(ctl + ["-n", ns, "get", "secrets", "-l", "owner=helm,name=" + name,
                                                        "-o", "json"]))
        return len(out["items"])

    def objects():
        found = {}
        for kind in ("configmap", "secret"):
            got = subprocess.run(ctl + ["-n", ns, "get", kind, name, "-o", "json", "--ignore-not-found"],
                                 capture_output=True, text=True, check=True).stdout
            found[kind] = json.loads(got)["data"] if got else None
        return found

    def content():
        found = objects()
        return found["configmap"], base64.b64decode(found["secret"]["message"]).decode()

    approve("before", "first")
    subprocess.run(helm + ["install", name, str(archive), "--values", str(values)], check=True, stdout=subprocess.DEVNULL)
    assert status() == (1, "deployed")
    approve("after", "first")
    reconcile("apply")
    assert status() == (2, "deployed") and content() == ({"value": "after", "literal": LITERAL}, "first")
    reconcile("apply")
    assert status() == (2, "deployed") and records() == 2, "A no-op apply added a revision"
    # --suppress-secrets hides Secret data in the diff output. It must not hide the change itself.
    approve("after", "second")
    reconcile("apply")
    assert status() == (3, "deployed") and content()[1] == "second", "Apply missed a Secret-only change"
    force_status(ns, "failed", name)
    reconcile("sync")
    assert status() == (4, "deployed") and records() == history, "Sync skipped a failed release or kept too much history"
    # Helmfile 1.8.4 and later: apply also upgrades a release that is not deployed, even with an empty diff.
    force_status(ns, "failed", name)
    diff = subprocess.run(["helm", "diff", "upgrade", name, str(archive), "-n", ns, "--values", str(values),
                           "--kubeconfig", str(cfg), "--kube-context", context, "--detailed-exitcode"],
                          stdout=subprocess.DEVNULL)
    assert diff.returncode == 0, "The failed release must have an empty diff"
    reconcile("apply")
    assert status() == (5, "deployed"), "Apply skipped a failed release with an empty diff"
    assert records() == history, "historyMax was not applied"
    assert content() == ({"value": "after", "literal": LITERAL}, "second")
    subprocess.run(helm + ["uninstall", name], check=True, stdout=subprocess.DEVNULL)
    assert records() == 0 and objects() == {"configmap": None, "secret": None}
    for method in ("apply", "sync"):
        refuse(method)
        assert records() == 0 and objects() == {"configmap": None, "secret": None}, f"{method} wrote a missing release"
    return {"changedApplyVerified": True, "noOpRevisionUnchanged": True, "approvedSecretOnlyChangeApplied": True,
            "failedEmptyDiffSynced": True, "failedEmptyDiffApplied": True, "historyLimitRespected": True,
            "missingReleaseRefused": True, "refValuesPassedToHelm": True}


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
    rows = [{"id": ns + "/" + name} for ns in namespaces]

    def latest(ns):
        return json.loads(subprocess.check_output(helm + ["-n", ns, "status", name, "-o", "json"]))

    def statuses():
        # The same input the deploy workflow reads: every release's latest status, from Helm's storage.
        listing = json.loads(subprocess.check_output(helm + ["list", "--all", "--all-namespaces", "-o", "json"]))
        return release_phases.helm_statuses(listing)

    def force_test_status(ns, status, name=name):
        # Construct states left by interrupted transactions, including Helm's
        # storage index and its encoded status. Only the named fixture is used.
        revision = json.loads(subprocess.check_output(helm + ["-n", ns, "status", name, "-o", "json"]))["version"]
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
        result = release_phases.run(rows, statuses(), reconcile, healthy)
        assert result["verifiedStacks"] == ["oci", "otel"] and revisions() == [2, 1]
        assert latest(namespaces[0])["info"]["status"] == "deployed"
        ran.clear()
        release_phases.run(rows, statuses(), reconcile, healthy)
        assert revisions() == [2, 1] and ran == [("apply", namespaces[0]), ("verified", namespaces[0]),
                                                 ("apply", namespaces[1]), ("verified", namespaces[1])]
        ran.clear()
        def unavailable(stack):
            assert stack == "oci"
            raise RuntimeError("fixture OCI health unavailable")
        try:
            release_phases.run(rows, statuses(), reconcile, unavailable)
        except RuntimeError as exc:
            assert str(exc) == "fixture OCI health unavailable"
        else:
            raise AssertionError("Failed OCI health accepted")
        assert ran == [("apply", namespaces[0])] and revisions() == [2, 1]
        ran.clear()
        missing = {"id": namespaces[1] + "/jaeger-phase-missing"}
        try:
            release_phases.run(rows + [missing], statuses(), reconcile, healthy)
        except ValueError as exc:
            assert "operator review" in str(exc)
        else:
            raise AssertionError("Missing application release accepted")
        assert not ran and revisions() == [2, 1]
        force_test_status(namespaces[1], "pending-upgrade")
        try:
            release_phases.run(rows, statuses(), reconcile, healthy)
        except ValueError as exc:
            assert "operator review" in str(exc)
        else:
            raise AssertionError("Pending OTel state accepted")
        assert not ran and revisions() == [2, 1]
        direct = direct_transactions(cfg, context, root, force_test_status)
        print(json.dumps({"failedEmptyDiffRecovered": True, "noOpNewRevisions": 0,
                          "ociHealthFailureBlockedOTel": True, "pendingBlockedAllReleases": True,
                          "missingReleaseBlockedAllReleases": True, **direct}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", choices=("fixture", "kind-jaeger-demo-adoption-fixture"), required=True)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Enable fixture assertions")
    exercise(args.kubeconfig, args.context)
