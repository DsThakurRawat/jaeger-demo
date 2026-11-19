# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Local synthetic observations and real chart/parameter preflight, no production inputs."""

import base64
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[4]
sys.path[:0] = [str(ROOT / "scripts/demo"), str(ROOT / "ops/executor")]
import approval_policies as ap
import permissions as p
import scheduled_adapter as a
import scheduled_approval_types as types
import scheduled_approvals as approvals
import scheduled_bundle as bundle
import scheduled_inputs
import yaml
from all_five_candidates import capacity_parts, capacity_states, fixture_images


def run(cfg, scratch, kube, packages, chart_root):
    slot = "2026-11-19T13:00:00Z"
    rawcfg = json.loads(kube(["config", "view", "--raw", "--flatten", "--minify", "-o", "json"]).stdout)
    server = urlparse(rawcfg["clusters"][0]["cluster"]["server"])
    assert (
        server.scheme == "https" and server.hostname == "127.0.0.1" and rawcfg["current-context"] == "fixture"
    )
    host = server.netloc
    commit = subprocess.check_output(["git", "-C", a.ROOT, "rev-parse", "HEAD"], text=True).strip()
    helper = shutil.which("jaeger-helm-preview")
    assert helper, "Place the pinned Helm patch preview helper on PATH"
    locks = json.loads((a.ROOT / scheduled_inputs.LOCK).read_bytes())
    chart_sha = re.search(
        r'^JAEGER_CHART_COMMIT="([a-f0-9]{40})"$', (a.ROOT / "scripts/demo/jaeger-chart.sh").read_text(), re.M
    )[1]
    assert (
        subprocess.check_output(["git", "-C", chart_root, "rev-parse", "HEAD"], text=True).strip()
        == chart_sha
    )
    chart_source = chart_root / "charts/jaeger"
    assert not any(path.is_symlink() for path in chart_source.rglob("*"))
    assert not subprocess.check_output(["git", "-C", chart_root, "status", "--porcelain", "--", "charts/jaeger"])
    packaged = {
        release: packages / f"{name}-{scheduled_inputs.pin(a.ROOT, variable)}.tgz"
        for release, variable, name, _ in scheduled_inputs.CHARTS
    }
    scheduled_inputs.checked_archives(list(packaged.values()), locks["charts"])
    scheduled_inputs.checked_archives(list((chart_source / "charts").iterdir()), locks["jaegerDependencies"])
    actual_ids = set()
    images = fixture_images()

    def create(obj):
        return json.loads(
            kube(["create", "--validate=false", "-f", "-", "-o", "json"], body=json.dumps(obj)).stdout
        )

    def install(items):
        if items:
            kube(
                ["create", "--validate=false", "-f", "-"],
                body=json.dumps({"apiVersion": "v1", "kind": "List", "items": list(items)}),
            )

    for namespace in ("jaeger", "opensearch", "otel-demo", p.OPS):
        create(p.resource("v1", "Namespace", namespace))
    for who in (p.PREPARER, p.EXECUTOR):
        create(p.resource("v1", "ServiceAccount", who, p.OPS))
    req = create(p.resource("v1", "ConfigMap", "daily-hosted-update", p.OPS, data={"state": "empty"}))
    cp = p.resource("v1", "ConfigMap", p.CHECKPOINT, "default", data={})
    cp["metadata"]["annotations"] = {p.PROTOCOL: "v1"}
    create(cp)
    install(ap.schema())
    kube(
        ["wait", "--for=condition=Established", "crd/" + types.RESOURCE + "." + types.GROUP, "--timeout=30s"]
    )
    install(ap.seeds())
    guards, grants = p.regular(daily=True)
    install(ap.stages("approval-guards") + guards + p.history_guard())
    install(ap.stages("approval-grants") + grants + list(p.history_grants()) + list(p.read_access()))
    install(
        p.bindings(
            "fixture-request-actors",
            p.OPS,
            [p.rule("", "configmaps", ["get", "patch"], ["daily-hosted-update"])],
            (p.PREPARER, p.EXECUTOR),
        )
    )
    projection = scratch / "projection"
    projection.mkdir()
    (projection / "ca.crt").write_bytes(
        base64.b64decode(rawcfg["clusters"][0]["cluster"]["certificate-authority-data"])
    )
    clients = {}
    for account in (p.PREPARER, p.EXECUTOR):
        token = kube(["-n", p.OPS, "create", "token", account, "--duration=20m"]).stdout.strip()
        if account == p.PREPARER:
            (projection / "token").write_text(token)
        rawcfg["users"] = [{"name": account, "user": {"token": token}}]
        rawcfg["contexts"][0]["context"]["user"] = account
        clients[account] = scratch / ("kubeconfig-" + account)
        clients[account].write_text(json.dumps(rawcfg))
    prep = clients[p.PREPARER]
    env = {"PATH": os.environ["PATH"], "KUBECONFIG": str(cfg), "HOME": str(scratch)}
    a.command(["helm", "package", chart_root / "charts/jaeger", "--destination", scratch], env=env)
    (local,) = scratch.glob("jaeger-*.tgz")
    charts = {}
    previous = {}
    candidates = {}
    lives = {}
    seen_refs = {}
    capacity = {}
    crds = a.docs(
        a.command(["helm", "show", "crds", packaged["default/prometheus"]], env=env)
    )
    for crd in crds:
        kube(["apply", "--server-side", "-f", "-"], body=a.encode(crd).decode())
        actual_ids.add(a.identity(crd, "default"))
    kube(["wait", "crd", "--all", "--for=condition=Established", "--timeout=40s"])
    for key, (ns, name, _) in a.RELEASES.items():
        pkg = local if key.endswith("-jaeger") else packaged[f"{ns}/{name}"]
        charts[ns + "/" + name] = pkg.read_bytes()
        metadata = yaml.safe_load(a.command(["helm", "show", "chart", pkg], env=env))
        values = a.values_for(key, a.ROOT, images)
        if key == "oci-monitoring":
            a.override(values, ("grafana", "adminPassword"), "synthetic-fixture-only")

        def render(v):
            return a.docs(
                a.command(
                    [
                        "helm",
                        "template",
                        name,
                        pkg,
                        "-n",
                        ns,
                        "--kube-version=1.36.1",
                        "--is-upgrade",
                        "-f",
                        "-",
                    ],
                    body=a.encode(v),
                    env=env,
                )
            )

        current = render(values)
        prior = copy.deepcopy(current)
        if key.endswith("-jaeger"):
            for obj in prior:
                if obj["kind"] == "Deployment":
                    for c in obj["spec"]["template"]["spec"]["containers"]:
                        c["image"] = c["image"].split(":")[0] + ":" + "c" * 40
        else:
            # Exercise one real, eligible ordinary change per other release, without touching any privilege/scope.
            for obj in prior:
                ident = approvals.resource_id(obj, ns)
                if ident not in types.allowed():
                    continue
                if obj["kind"] == "ConfigMap" and obj.get("data"):
                    obj["data"]["fixture-previous-key"] = "synthetic-prior"
                    break
                if obj["kind"] == "Deployment" and obj["metadata"]["name"] == "opensearch-dashboards":
                    obj["spec"]["template"]["spec"]["containers"][0]["image"] = (
                        "opensearchproject/opensearch-dashboards:3.6.0"
                    )
                    break
            else:
                raise AssertionError("no safe change for " + key)
        method = "csa" if key in ("otel-jaeger", "opensearch", "otel-demo") else "ssa"

        def release(objects):
            return {
                "name": name,
                "namespace": ns,
                "apply_method": method,
                "manifest": "\n---\n".join(json.dumps(o) for o in objects),
                "config": values,
                "info": {"status": "deployed"},
                "chart": {
                    "metadata": {field: metadata[field] for field in ("name", "version", "appVersion")}
                },
            }

        previous[key] = release(prior)
        candidates[key] = release(current)
        idx = a.index(current, ns)
        old = a.index(prior, ns)
        suppl = a.supplemental_for(key, a.ROOT)
        targets = a.inventory(key, prior, suppl)
        extras = set(a.index(suppl, ns))
        capacity.update(capacity_states(key, targets, suppl))
        for ident, o in targets.items():
            o = copy.deepcopy(o)
            m = o["metadata"]
            m["namespace"] = ident.split(":")[2]
            if ident.split(":")[2] == "-":
                m.pop("namespace")
            if ident in extras:
                m.setdefault("annotations", {})[a.LAST_APPLIED] = json.dumps(copy.deepcopy(targets[ident]))
            else:
                m.setdefault("labels", {})["app.kubernetes.io/managed-by"] = "Helm"
                annotations = m.get("annotations") or {}
                m["annotations"] = annotations
                annotations.update({"meta.helm.sh/release-name": name, "meta.helm.sh/release-namespace": ns})
            mutation = (ident in extras and o["kind"] == "Deployment") or (
                ident not in extras and old[ident] != idx[ident]
            )
            if mutation or approvals.resource_id(o, ns) in types.allowed():
                live = json.loads(
                    kube(
                        ["apply", "--server-side", "--field-manager=helm", "-f", "-", "-o", "json"],
                        body=json.dumps(o),
                    ).stdout
                )
                actual_ids.add(ident)
            else:
                m.update(uid=str(uuid.uuid4()), resourceVersion="1")
                if "webhooks" in o:
                    for wh in o["webhooks"]:
                        wh["clientConfig"]["caBundle"] = "ZmFrZQ=="
                live = o
            lives[ident] = live
        if key.endswith("-jaeger"):
            seen_refs[key] = {
                role: {
                    "reference": old[f"apps/v1:Deployment:{ns}:{n}"]["spec"]["template"]["spec"][
                        "containers"
                    ][0]["image"].removeprefix("docker.io/"),
                    "runtimeDigests": ["sha256:" + "e" * 64],
                }
                for role, n in (("jaeger", "jaeger"), ("hotrod", "jaeger-hotrod"))
            }
    cert = "v1:Secret:default:prometheus-kube-prometheus-admission"
    lives[cert] = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": "prometheus-kube-prometheus-admission",
            "namespace": "default",
            "uid": "fixture-admission-cert",
        },
        "data": {"ca": "ZmFrZQ=="},
    }
    protected_get = a.get_object
    ctx = {"clusterUID": "fixture", "kubernetes": "v1.36.1"}
    records = {
        k: [{"revision": 5, "status": "deployed", "uid": "fixture-" + k, "sha256": a.sha(a.encode(v))}]
        for k, v in previous.items()
    }
    original_apply = approvals.apply_patch

    def real_apply(ident, version, target):
        return original_apply(ident, version, target, authority=host, account=projection)

    with ExitStack() as stack:

        def fake(owner, attr, fn):
            stack.enter_context(patch.object(owner, attr, side_effect=fn))

        fake(a, "history", lambda k: (records[k], previous[k], previous[k]))
        fake(
            a,
            "get_object",
            lambda ident: protected_get(ident) if ident in actual_ids else copy.deepcopy(lives[ident]),
        )
        fake(a, "check_context", lambda: ctx)
        fake(a, "checkpoint", lambda: ("fixture-checkpoint", {k: None for k in a.COMPONENTS}))
        fake(a, "bindings", lambda: ["fixture-pvc", "uid", "pv"])
        fake(a, "snapshot_pods", lambda k: seen_refs[k])
        fake(a, "helm_transaction", lambda key, *_, **__: candidates[key])
        fake(approvals, "apply_patch", real_apply)
        stack.enter_context(patch.object(approvals, "CSA_HELPER", helper))
        stack.enter_context(patch.dict(os.environ, {"KUBECONFIG": str(prep)}))
        files = a.prepare_inputs(slot, commit, a.ROOT, charts, images)
        packed = bundle.pack(files)
        assert bundle.unpack(packed) == files
        receipt = {"configurationSHA": commit, "bundleSHA256": hashlib.sha256(packed).hexdigest()}
        dig = approvals.publish_approvals(files, slot, receipt)
        assert dig == approvals.aggregate(slot, receipt, req["metadata"]["uid"])
        counts = {}
        part_bytes = {}
        targets = {}
        for scope in types.PARTITIONS:
            s = approvals.get(approvals.TYPE, types.name(scope), types.OPS)["spec"]
            counts[scope] = len(s["changes"])
            part_bytes[scope] = len(types.canonical(s))
            targets[scope] = list(sorted(s["changes"]))
        result = {
            "configurationSHA": commit,
            "testedSources": {
                str(path.relative_to(ROOT)): a.sha(path.read_bytes())
                for path in (
                    Path(__file__),
                    Path(__file__).with_name("all_five_candidates.py"),
                    ROOT / "scripts/demo/scheduled_bundle.py",
                    ROOT / "scripts/demo/scheduled_approvals.py",
                    ROOT / "scripts/demo/scheduled_approval_types.py",
                    ROOT / "scripts/demo/scheduled_adapter.py",
                    ROOT / "ops/executor/approval_policies.py",
                    ROOT / "ops/executor/permissions.py",
                )
            },
            "members": len(files),
            "largestMember": max(files, key=lambda k: len(files[k])),
            "largestMemberBytes": max(map(len, files.values())),
            "archiveBytes": len(packed),
            "selected": json.loads(files["plan.json"])["selected"],
            "partitionCounts": counts,
            "partitionBytes": part_bytes,
            "targets": targets,
            "syntheticCapture": True,
            "nativePreparerDryRunAndPartitionWrites": True,
        }
        # Prior drift in an otherwise unchanged chart would be repaired by actual SSA.
        ident = "v1:ConfigMap:default:user-config"
        baseline = lives[ident]
        previous_content = copy.deepcopy(baseline["data"])
        data = copy.deepcopy(previous_content)
        data[next(iter(data))] += "\n# simulated out-of-band drift"

        def change_baseline(field_data):
            body = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": types.variant(baseline)["metadata"],
                "data": field_data,
            }
            return json.loads(
                kube(
                    ["apply", "--server-side", "--field-manager=helm", "-f", "-", "-o", "json"],
                    body=json.dumps(body),
                ).stdout
            )

        lives[ident] = change_baseline(data)
        drifted_files = a.prepare_inputs(slot, commit, a.ROOT, charts, images)
        old_parts = approvals.aggregate(slot, receipt, req["metadata"]["uid"])
        try:
            approvals.publish_approvals(drifted_files, slot, receipt)
        except ValueError as exc:
            assert "Unchanged chart input" in str(exc), str(exc)
        else:
            raise AssertionError("Unchanged-source drift unexpectedly passed")
        assert approvals.aggregate(slot, receipt, req["metadata"]["uid"]) == old_parts
        lives[ident] = change_baseline(previous_content)
        result["priorDriftBlockedBeforePublication"] = True
        result["broad"] = verify_broad_staging(
            slot, receipt, req["metadata"]["uid"], lives, capacity, clients
        )
        return result


def verify_broad_staging(slot, receipt, request_uid, lives, capacity, clients):
    # Broad native staging and stateful update at the largest reviewed dashboard.
    all_parts = capacity_parts(capacity, 3)
    name = "prometheus-kube-prometheus-k8s-resources-namespace"
    handle = "core/configmaps/" + name
    dashboard = lives["v1:ConfigMap:default:" + name]
    variants = []
    for marker in (None, "reviewed", "rollback"):
        body = copy.deepcopy(dashboard)
        if marker:
            body["metadata"]["labels"]["fixture-revision"] = marker
        variants.append(types.variant(body))
    all_parts[types.partition("default", handle)]["changes"][handle] = {
        "uid": dashboard["metadata"]["uid"],
        "variants": variants,
    }
    with patch.object(
        approvals, "prepare_variants", return_value={k: v["changes"] for k, v in all_parts.items()}
    ):
        broad = approvals.publish_approvals({}, slot, receipt)
        assert broad == approvals.aggregate(slot, receipt, request_uid)

    def stage(user, ns, kind, n, val, check=True):
        result = subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(clients[user]),
                "--context",
                "fixture",
                "-n",
                ns,
                "patch",
                kind,
                n,
                "--type=merge",
                "--patch-file=/dev/stdin",
            ],
            input=json.dumps(val),
            text=True,
            capture_output=True,
            timeout=45,
        )
        if check:
            assert result.returncode == 0, "Scoped control or dashboard operation failed"
        return result

    stage(
        p.PREPARER,
        p.OPS,
        "cm",
        "daily-hosted-update",
        {"data": dict(state="ready", engine="scripts-v1", slot=slot, **receipt)},
    )
    stage(p.EXECUTOR, p.OPS, "cm", "daily-hosted-update", {"data": {"state": "requested"}})
    stage(
        p.EXECUTOR,
        "default",
        "cm",
        p.CHECKPOINT,
        {"metadata": {"annotations": {p.HOLDER: "daily/" + slot + "/" + request_uid}}},
    )
    stage(p.EXECUTOR, p.OPS, "cm", "daily-hosted-update", {"data": {"state": "running"}})
    stage(p.EXECUTOR, "default", "cm", name, {"metadata": {"labels": {"fixture-revision": "reviewed"}}})
    denied = stage(
        p.EXECUTOR,
        "default",
        "cm",
        name,
        {"metadata": {"labels": {"fixture-revision": "unreviewed"}}},
        check=False,
    )
    assert denied.returncode != 0 and "Unreviewed change" in denied.stderr, denied.stderr[:300]
    stage(p.EXECUTOR, "default", "cm", name, {"metadata": {"labels": {"fixture-revision": "rollback"}}})
    return {
        "nativePartitions": len(all_parts),
        "changes": sum(len(v["changes"]) for v in all_parts.values()),
        "largest": max(len(types.canonical(v)) for v in all_parts.values()),
        "largeDashboard": handle,
        "nativeForwardRollbackPassed": True,
        "unapprovedRejected": True,
    }
