#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Native approval positive/negative cases using only a disposable loopback API."""

import argparse
import base64
import copy
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import time
from urllib.parse import urlsplit
from unittest.mock import patch as replace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import permissions as p
import approval_policies as ap
import scheduled_approval_types as t
import scheduled_approvals as app


def exercise(cfg, context):
    admin = ["kubectl", "--kubeconfig", str(cfg), "--context", context]

    def call(client, *args, doc=None, denied=None):
        result = subprocess.run(
            client + list(args),
            input=t.canonical(doc) if doc is not None else None,
            capture_output=True,
            timeout=90,
        )
        if denied is not None:
            assert result.returncode and denied in result.stderr.decode(), result.stderr.decode()[:900]
            return
        assert not result.returncode, result.stderr.decode()[:900]
        return json.loads(result.stdout) if result.stdout.startswith(b"{") else result.stdout

    view = call(admin, "config", "view", "--minify", "-o", "json")
    assert urlsplit(view["clusters"][0]["cluster"]["server"]).hostname == "127.0.0.1"

    def create(o):
        return call(admin, "create", "-f", "-", "-o", "json", doc=o)

    def install(items):
        if items:
            call(admin, "create", "-f", "-", doc={"apiVersion": "v1", "kind": "List", "items": items})

    create(p.resource("v1", "Namespace", p.OPS))
    for n in ("jaeger", "opensearch", "otel-demo"):
        create(p.resource("v1", "Namespace", n))
    for who in (p.PREPARER, p.EXECUTOR):
        create(p.resource("v1", "ServiceAccount", who, p.OPS))
    slot = "2026-11-19T13:00:00Z"
    receipt = {"configurationSHA": "a" * 40, "bundleSHA256": "b" * 64}
    req = create(p.resource("v1", "ConfigMap", "daily-hosted-update", p.OPS, data={"state": "empty"}))
    uid = req["metadata"]["uid"]
    cp = p.resource("v1", "ConfigMap", p.CHECKPOINT, "default", data={})
    cp["metadata"]["annotations"] = {p.PROTOCOL: "v1"}
    create(cp)
    refs = [
        ("default", "user-config"),
        ("default", "prometheus-grafana-config-dashboards"),
        ("jaeger", "user-config"),
        ("opensearch", "opensearch-cluster-single-config"),
        ("otel-demo", "flagd-config"),
    ]
    assert all((ns, "", "configmaps", name) in t.allowed() for ns, name in refs)
    live = [create(p.resource("v1", "ConfigMap", n, ns, data={"settings": "existing"})) for ns, n in refs]
    install(ap.schema())
    call(admin, "wait", "--for=condition=Established", "crd/" + t.RESOURCE + "." + t.GROUP, "--timeout=30s")
    install(ap.seeds())
    guards, grants = p.regular(daily=True)
    install(ap.stages("approval-guards") + guards + p.history_guard())
    install(ap.stages("approval-grants") + grants + list(p.history_grants()) + list(p.read_access()))
    request_roles = p.bindings(
        "fixture-request-actors",
        p.OPS,
        [p.rule("", "configmaps", ["get", "patch"], ["daily-hosted-update"])],
        (p.PREPARER, p.EXECUTOR),
    )
    install(request_roles)

    def patch(client, ns, kind, n, obj, *, dry=False, denied=None):
        return call(
            client,
            "-n",
            ns,
            "patch",
            kind,
            n,
            "--type=merge",
            "--patch-file=/dev/stdin",
            "-o",
            "json",
            *(["--dry-run=server"] if dry else []),
            doc=obj,
            denied=denied,
        )

    with tempfile.TemporaryDirectory() as tmp:

        def client(who):
            token = call(admin, "-n", p.OPS, "create", "token", who, "--duration=10m").decode().strip()
            conf = call(admin, "config", "view", "--raw", "--minify", "-o", "json")
            conf["users"] = [{"name": "fixture", "user": {"token": token}}]
            conf["contexts"][0]["context"]["user"] = "fixture"
            dest = Path(tmp) / who
            dest.write_bytes(t.canonical(conf))
            dest.chmod(0o600)
            return ["kubectl", "--kubeconfig", str(dest), "--context", context]

        prep, actor = client(p.PREPARER), client(p.EXECUTOR)
        time.sleep(1)
        for ns in ("default", "jaeger", "otel-demo", "opensearch"):
            for verb, res in (("list", "pods"), ("get", "replicasets.apps/example")):
                result = subprocess.run(prep + ["auth", "can-i", verb, res, "-n", ns], capture_output=True)
                assert (result.stdout.strip() == b"yes") == (ns in ("default", "jaeger"))
        account = Path(tmp) / "projection"
        account.mkdir(mode=0o700)
        primary = call(admin, "config", "view", "--raw", "--flatten", "--minify", "-o", "json")
        (account / "ca.crt").write_bytes(
            base64.b64decode(primary["clusters"][0]["cluster"]["certificate-authority-data"])
        )
        (account / "token").write_text(
            json.loads((Path(tmp) / p.PREPARER).read_text())["users"][0]["user"]["token"]
        )
        host = urlsplit(primary["clusters"][0]["cluster"]["server"]).netloc

        class FixtureAdapter:
            @staticmethod
            def kube(*args, body=None):
                out = subprocess.run(prep + list(args), input=body, capture_output=True)
                if out.returncode:
                    raise ValueError("Private fixture Kubernetes call failed")
                return out.stdout

            @staticmethod
            def command(args, body=None):
                assert args == [app.CSA_HELPER]
                out = subprocess.run([shutil.which("jaeger-helm-preview")], input=body, capture_output=True)
                if out.returncode:
                    raise ValueError("Private fixture helper failed")
                return out.stdout

            @staticmethod
            def tests(obj):
                return [
                    {"op": "test", "path": "/metadata/" + k, "value": obj["metadata"][k]}
                    for k in ("uid", "resourceVersion")
                ]

        specs = {s: dict(scope=s, slot=slot, requestUID=uid, changes={}, **receipt) for s in t.SCOPES}
        for (ns, n), old in zip(refs, live):
            candidate = patch(prep, ns, "configmap", n, {"data": {"settings": "changed"}}, dry=True)
            patch(prep, ns, "configmap", n, {"data": {"settings": "changed"}}, denied="server-side dry-run")
            specs[ns]["changes"]["core/configmaps/" + n] = {
                "uid": old["metadata"]["uid"],
                "variants": [t.variant(old), t.variant(candidate)],
            }
        # Compare both real Helm algorithms' prospective output with a real upgrade.
        # These synthetic chart objects belong only to this fresh loopback server.
        helm_objects = []
        for i, (ns, method) in enumerate((("default", "ssa"), ("jaeger", "csa"))):
            release = "jaeger"
            kind = "Deployment"
            n = "jaeger"
            doc = p.resource(
                "apps/v1",
                kind,
                n,
                ns,
                spec={
                    "selector": {"matchLabels": {"app": n}},
                    "template": {
                        "metadata": {"labels": {"app": n}},
                        "spec": {"containers": [{"name": n, "image": "registry.invalid/original"}]},
                    },
                },
            )
            chart = Path(tmp) / ("chart-" + method)
            (chart / "templates").mkdir(parents=True)
            (chart / "Chart.yaml").write_text("apiVersion: v2\nname: jaeger\nversion: 1.0.0\n")
            tmpl = chart / "templates/app.yaml"
            tmpl.write_bytes(t.canonical(doc))
            helm = ["helm", "--kubeconfig", str(cfg), "-n", ns]
            call(
                helm,
                "install",
                release,
                str(chart),
                "--server-side=" + ("true" if method == "ssa" else "false"),
                "--wait=hookOnly",
            )
            before = call(prep, "-n", ns, "get", "deployment", n, "-o", "json")
            desired = copy.deepcopy(doc)
            desired["spec"]["template"]["spec"]["containers"][0]["image"] = "registry.invalid/reviewed"
            original_apply = app.apply_patch

            def local_apply(ident, version, obj):
                return original_apply(ident, version, obj, authority=host, account=account)

            with (
                replace.object(app, "client", return_value=FixtureAdapter),
                replace.object(app, "apply_patch", side_effect=local_apply),
            ):
                candidate = app.preview(doc, desired, before, ns, release, method)
            app.safe_transition(before, candidate)
            specs[ns]["changes"]["apps/deployments/" + n] = {
                "uid": before["metadata"]["uid"],
                "variants": [t.variant(before), t.variant(candidate)],
            }
            tmpl.write_bytes(t.canonical(desired))
            helm_objects.append((ns, release, chart, n, before, candidate))
        ready = {"data": dict(state="ready", engine="scripts-v1", slot=slot, **receipt)}
        for scope in t.SCOPES[:-1]:
            patch(prep, p.OPS, t.RESOURCE, t.name(scope), {"spec": specs[scope]})
        patch(prep, p.OPS, "cm", "daily-hosted-update", ready, denied="Every daily parameter")
        # The public receipt goes into the control plane only after readback of every
        # UID and full typed spec. The isolated fixture supplies synthetic variants.
        with (
            replace.object(app, "client", return_value=FixtureAdapter),
            replace.object(
                app,
                "prepare_variants",
                return_value={scope: value["changes"] for scope, value in specs.items()},
            ),
        ):
            commitment = app.publish_approvals({}, slot, receipt)
            assert commitment == app.aggregate(slot, receipt, uid)
            assert app.publish_approvals({}, slot, receipt) == commitment
        patch(prep, p.OPS, "cm", "daily-hosted-update", ready)
        patch(
            prep,
            p.OPS,
            t.RESOURCE,
            t.name("default"),
            {"spec": {"bundleSHA256": "f" * 64}},
            denied="before advertisement",
        )
        patch(actor, p.OPS, "cm", "daily-hosted-update", {"data": {"state": "requested"}})
        patch(
            actor,
            "default",
            "cm",
            p.CHECKPOINT,
            {"metadata": {"annotations": {p.HOLDER: "daily/" + slot + "/" + uid}}},
        )
        patch(actor, p.OPS, "cm", "daily-hosted-update", {"data": {"state": "running"}})
        # Sender here uses actual executor token, history/patch guards and the inherited Helm mode.
        for ns, release, chart, n, before, candidate in helm_objects:
            helm = ["helm", "--kubeconfig", str(Path(tmp) / p.EXECUTOR), "-n", ns]
            call(helm, "upgrade", release, str(chart), "--server-side=auto", "--wait=hookOnly")
            changed = call(admin, "-n", ns, "get", "deployment", n, "-o", "json")
            assert t.variant(changed) == t.variant(candidate)
            call(helm, "rollback", release, "1", "--wait=hookOnly")
            restored = call(admin, "-n", ns, "get", "deployment", n, "-o", "json")
            assert t.variant(restored) == t.variant(before)
        for ns, n in refs:
            patch(actor, ns, "cm", n, {"data": {"settings": "changed"}})
            patch(actor, ns, "cm", n, {"data": {"settings": "unreviewed"}}, denied="Unreviewed change")
            patch(actor, ns, "cm", n, {"metadata": {"labels": {"new": "bad"}}}, denied="Unreviewed change")
            patch(actor, ns, "cm", n, {"data": {"settings": "existing"}})
        print(
            json.dumps(
                {
                    "fiveParameterBindings": True,
                    "fourNamespaces": True,
                    "nativeDryRun": True,
                    "exactForwardAndRollback": True,
                    "realHelmSSAAndCSA": True,
                    "preparerPodAndReplicaSetScope": True,
                    "partialOrSubstitutedPreparationDenied": True,
                }
            )
        )


if __name__ == "__main__":
    args = argparse.ArgumentParser()
    args.add_argument("--kubeconfig", required=True, type=Path)
    args.add_argument("--context", required=True, choices=["fixture"])
    parsed = args.parse_args()
    if not __debug__:
        args.error("Enable fixture assertions")
    exercise(parsed.kubeconfig, parsed.context)
