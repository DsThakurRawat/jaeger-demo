#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Verify native Helmfile transactions against a fresh disposable loopback API server."""

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
from adoption_bundle import BundleError
from helmfile_transaction import reconcile_existing


def exercise(cfg, context):
    ctl = ["kubectl", "--kubeconfig", str(cfg), "--context", context]
    helm = ["helm", "--kubeconfig", str(cfg), "--kube-context", context, "-n", "default"]
    def run(args, data=None, check=True):
        return subprocess.run(args, input=data, capture_output=True, check=check)
    view = json.loads(run(ctl + ["config", "view", "--minify", "-o", "json"]).stdout)
    if urlsplit(view["clusters"][0]["cluster"]["server"]).hostname != "127.0.0.1":
        raise ValueError("A disposable loopback API server is required")
    def records():
        items = json.loads(run(ctl + ["-n", "default", "get", "secrets", "-l", "owner=helm,name=jaeger", "-o", "json"]).stdout)
        return len(items["items"])
    if records() or run(ctl + ["get", "cm", "jaeger-transaction-fixture"], check=False).returncode == 0:
        raise ValueError("Release or ConfigMap already exists on fixture cluster")
    def status():
        out = json.loads(run(helm + ["status", "jaeger", "-o", "json"]).stdout)
        return out["version"], out["info"]["status"]
    def content():
        return json.loads(run(ctl + ["get", "cm", "jaeger-transaction-fixture", "-o", "json"]).stdout)["data"]["value"]
    def set_failed():
        secret = f"sh.helm.release.v1.jaeger.v{status()[0]}"
        old = json.loads(run(ctl + ["-n", "default", "get", "secret", secret, "-o", "json"]).stdout)
        record = json.loads(gzip.decompress(base64.b64decode(base64.b64decode(old["data"]["release"]))))
        assert (record["name"], record["namespace"], record["version"]) == ("jaeger", "default", status()[0])
        record["info"]["status"] = "failed"
        old["data"]["release"] = base64.b64encode(base64.b64encode(gzip.compress(json.dumps(record).encode()))).decode()
        old["metadata"]["labels"]["status"] = "failed"
        run(ctl + ["replace", "-f", "-"], json.dumps(old).encode())

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        chart = root / "chart"
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text(
            "apiVersion: v2\nname: local-fixture\nversion: 1.0.0\ndependencies:\n"
            "  - {name: child, version: 1.0.0, repository: 'https://127.0.0.1:1/unreachable'}\n")
        child = chart / "charts/child"
        child.mkdir(parents=True)
        (child / "Chart.yaml").write_text("apiVersion: v2\nname: child\nversion: 1.0.0\n")
        (chart / "templates" / "config.yaml").write_text(
            'apiVersion: v1\nkind: ConfigMap\nmetadata: {name: jaeger-transaction-fixture}\ndata: {value: {{ .Values.value | quote }}}\n')
        (chart / "templates" / "secret.yaml").write_text(
            'apiVersion: v1\nkind: Secret\nmetadata: {name: jaeger-transaction-fixture}\ndata: {message: {{ .Values.sensitive | b64enc | quote }}}\n')
        run(helm + ["package", str(chart), "--destination", str(root)])
        archive = root / "local-fixture-1.0.0.tgz"
        run(helm + ["install", "jaeger", str(archive), "--set-string", "value=before,sensitive=first"])
        work = {"chart.tgz":archive.read_bytes()}
        values = root / "values.yaml"
        def approve(value, sensitive):
            work["values.yaml"] = yaml.safe_dump({"value":value, "sensitive":sensitive,
                                                  "literal":"ref+unused-provider://literal"}).encode()
            values.write_bytes(work["values.yaml"])
            work["postrender.yaml"] = run(helm + ["template", "jaeger", str(archive), "-f", str(values)]).stdout
        approve("after", "first")
        row = {"id":"default/jaeger", "helm":{"operation":"upgrade", "atomic":True, "wait":"legacy",
               "timeoutSeconds":45, "historyMax":3, "skipCRDs":False, "createNamespace":False}}
        def invoke(method):
            reconcile_existing(row, method, work, kubeconfig=cfg, context=context)
        def refuse(method):
            try:
                invoke(method)
            except BundleError:
                return
            raise AssertionError("Unexpected Helmfile success")
        work["values.yaml"] = b'value: "unapproved"\nsensitive: first\n'
        refuse("sync")
        assert status() == (1,"deployed") and content() == "before" and records() == 1
        refuse("apply")
        assert status() == (1,"deployed") and content() == "before" and records() == 1
        work["values.yaml"] = b'value: "before"\nsensitive: first\n'
        refuse("apply")
        assert status() == (1,"deployed") and content() == "before" and records() == 1
        work["values.yaml"] = values.read_bytes()
        invoke("apply")
        assert status() == (2,"deployed") and content() == "after"
        invoke("apply")
        assert status() == (2,"deployed") and records() == 2
        approve("after", "second")
        invoke("apply")
        secret = json.loads(run(ctl + ["get", "secret", "jaeger-transaction-fixture", "-o", "json"]).stdout)
        assert base64.b64decode(secret["data"]["message"]) == b"second" and status() == (3,"deployed"), "Secret-only diff missed"
        set_failed()
        invoke("sync")
        assert status() == (4,"deployed") and content() == "after" and records() == 3
        # Helmfile v1.8.4 and later: apply also upgrades a release that is not deployed, even with an empty diff.
        set_failed()
        invoke("apply")
        assert status() == (5,"deployed") and content() == "after" and records() == 3, "Apply skipped a failed release"
        run(helm + ["uninstall", "jaeger"])
        for method in ("apply","sync"):
            refuse(method)
            assert records() == 0 and run(ctl + ["get", "cm", "jaeger-transaction-fixture"], check=False).returncode
        print(json.dumps({"changedValuesRefusedBeforeMutation":True, "changedApplyVerified":True,
                          "noOpRevisionUnchanged":True, "failedEmptyDiffSynced":True, "failedEmptyDiffApplied":True,
                          "missingReleaseRefused":True,
                          "approvedSecretOnlyChangeApplied":True, "historyLimitRespected":True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", choices=("fixture","kind-jaeger-demo-adoption-fixture"), required=True)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Enable fixture assertions")
    exercise(args.kubeconfig.resolve(), args.context)
