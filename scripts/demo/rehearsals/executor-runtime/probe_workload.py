#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Verify the pinned executor image, Pods, service accounts and volumes on a fresh kind cluster."""

import argparse
from importlib import import_module
from datetime import timedelta
from ipaddress import ip_address
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
sys.path.insert(0, str(HERE.parent / "scheduled-request"))
access = import_module("access")
protocol = import_module("scheduled_request")
fixture_approvals = import_module("fixture_approvals")

NS = protocol.REQUEST[0]
EXECUTOR = "jaeger-demo-executor"
PREPARER = "jaeger-demo-preparer"
REQUESTER = "fixture-workload-requester"
CONTEXT = "kind-jaeger-executor-ci"


def run(args, *, doc=None, timeout=40):
    result = subprocess.run(
        args,
        input=None if doc is None else json.dumps(doc),
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        raise ValueError(result.stderr[-1400:] or "Fixture command failed")
    return result.stdout


def check_fixture(kube):
    cfg = json.loads(run([*kube, "config", "view", "--minify", "-o", "json"]))
    api = urlsplit(cfg["clusters"][0]["cluster"]["server"])
    if (
        cfg["current-context"] != CONTEXT
        or [c["name"] for c in cfg["clusters"]] != [CONTEXT]
        or [c["name"] for c in cfg["users"]] != [CONTEXT]
        or api.scheme != "https"
        or not api.port
        or not ip_address(api.hostname).is_loopback
        or api.username
        or api.password
    ):
        raise ValueError("Expected the dedicated kind loopback context")
    if run([*kube, "get", "nodes", "-o", "name"]).strip() != "node/jaeger-executor-ci-control-plane":
        raise ValueError("Unexpected local kind node")
    if (
        run([*kube, "get", "namespace", NS, "--ignore-not-found", "-o", "name"]).strip()
        or run(
            [
                *kube,
                "-n",
                "default",
                "get",
                "configmap",
                protocol.CHECKPOINT[1],
                "--ignore-not-found",
                "-o",
                "name",
            ]
        ).strip()
    ):
        raise ValueError("The workload rehearsal requires a fresh disposable cluster")


def add_fixtures(obj):
    name = obj["kind"]
    if name not in ("Deployment", "CronJob", "Job"):
        return
    job = obj["spec"]["jobTemplate"]["spec"] if name == "CronJob" else obj["spec"]
    pod = job["template"]["spec"]
    container = pod["containers"][0]
    container["imagePullPolicy"] = "Never"
    if name == "Job":
        # Exercise imports from the original image before mounting the synthetic application.
        container["args"][1] = (
            "import sys\nsys.path.insert(0,'/opt/jaeger-demo/scripts/demo')\n"
            "import scheduled_receiver, scheduled_bundle, scheduled_adapter, scheduled_inputs, scheduled_approvals\n"
            + container["args"][1]
        )
        return
    pod["volumes"].append({"name": "fixture", "configMap": {"name": "fixture-app"}})
    container["volumeMounts"].append({"name": "fixture", "mountPath": "/fixture", "readOnly": True})
    for module in ("scheduled_inputs.py", "scheduled_adapter.py", "scheduled_approvals.py"):
        container["volumeMounts"].append(
            {
                "name": "fixture",
                "subPath": module,
                "mountPath": "/opt/jaeger-demo/scripts/demo/" + module,
                "readOnly": True,
            }
        )
    container.setdefault("env", []).append(
        {"name": "PYTHONPATH", "value": "/fixture:/opt/jaeger-demo/scripts/demo"}
    )


def generate_keys(work):
    signer, decryptor = work / "signing-key.pem", work / "decryption-key.pem"
    for alg, private, public in (
        ("ED25519", signer, work / "verification-key.pem"),
        ("X25519", decryptor, work / "recipient-public.pem"),
    ):
        run(["openssl", "genpkey", "-algorithm", alg, "-out", str(private)])
        run(["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)])


def install_keys(kube, work):
    for role, names in (
        (PREPARER, ("signing-key.pem", "verification-key.pem", "recipient-public.pem")),
        (EXECUTOR, ("verification-key.pem", "decryption-key.pem")),
    ):
        run(
            [
                *kube,
                "-n",
                NS,
                "create",
                "secret",
                "generic",
                role + "-keys",
                *["--from-file=" + str(work / n) for n in names],
            ]
        )


def rehearse(kube, image, work):
    check_fixture(kube)
    generate_keys(work)

    def api(*args, doc=None, timeout=40):
        return run([*kube, *args], doc=doc, timeout=timeout)

    def ns(*args, **kwargs):
        return api("-n", NS, *args, **kwargs)

    def field(kind, name, path):
        return ns("get", kind, name, "-o", "jsonpath=" + path)

    def wait_state(value):
        ns(
            "wait",
            "--for=jsonpath={.data.state}=" + value,
            "configmap/" + protocol.REQUEST[1],
            "--timeout=120s",
            timeout=130,
        )

    def rollout():
        ns("rollout", "status", "deployment/" + EXECUTOR, "--timeout=3m", timeout=190)

    def wait_job(name):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            status = json.loads(ns("get", "job", name, "-o", "json")).get("status", {})
            conditions = {c["type"] for c in status.get("conditions", []) if c["status"] == "True"}
            if "Complete" in conditions:
                return
            if "Failed" in conditions:
                raise RuntimeError(f"Disposable Job {name} failed. Inspect its Pod logs.")
            time.sleep(2)
        raise TimeoutError(f"Disposable Job {name} did not finish")

    def outcome_records():
        raw = ns(
            "exec",
            "deployment/" + EXECUTOR,
            "--",
            "python3",
            "-c",
            "from pathlib import Path; import json; print(json.dumps([json.loads(p.read_bytes()) for p in Path('/var/lib/jaeger-outcomes').glob('*.json')]))",
        )
        return json.loads(raw)

    cases = []

    def check(description, value):
        if not value:
            raise AssertionError(description)
        cases.append(description)
        print(description, flush=True)

    objects = json.loads(
        run(
            [
                sys.executable,
                str(ROOT / "ops/executor/workloads.py"),
                "--image",
                image,
                "--storage-class",
                "standard",
                "--cluster-uid",
                api("get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}").strip(),
            ]
        )
    )["items"]
    api("create", "namespace", NS)
    api("-n", "default", "create", "configmap", protocol.CHECKPOINT[1], "--from-literal=fixture=fixed")
    api("-n", "default", "annotate", "configmap", protocol.CHECKPOINT[1], protocol.PROTOCOL + "=v1")
    principals = [
        REQUESTER,
        f"system:serviceaccount:{NS}:{PREPARER}",
        f"system:serviceaccount:{NS}:{EXECUTOR}",
        "fixture-workload-recovery",
    ]
    guards = list(access.manifests(*principals))
    guards.extend(fixture_approvals.resources(*protocol.REQUEST, principals[1]))
    for item in guards:
        if item["kind"] not in ("Namespace", "RoleBinding"):
            api("create", "-f", "-", doc=item)
    access.verify_barrier(
        lambda: subprocess.run(
            [*kube, *access.barrier_command()], capture_output=True, text=True, timeout=20
        )
    )
    for item in guards:
        if item["kind"] == "RoleBinding":
            api("create", "-f", "-", doc=item)
    api(
        "-n",
        "default",
        "create",
        "role",
        "fixture-checkpoint",
        "--resource=configmaps",
        "--verb=get,patch",
        "--resource-name=" + protocol.CHECKPOINT[1],
    )
    api(
        "-n",
        "default",
        "create",
        "rolebinding",
        "fixture-checkpoint",
        "--role=fixture-checkpoint",
        "--serviceaccount=" + NS + ":" + EXECUTOR,
    )
    install_keys(kube, work)
    slot = protocol.timestamp("2026-11-19T13:00:00Z")
    fixture = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "fixture-app", "namespace": NS},
        "data": {
            "slot": protocol.format_utc(slot),
            "outcome": "success",
            "scheduled_approvals.py": (HERE / "fixture_approvals.py").read_text(),
            **{
                f: (HERE / f).read_text()
                for f in ("scheduled_inputs.py", "scheduled_adapter.py", "sitecustomize.py")
            },
        },
    }
    api("create", "-f", "-", doc=fixture)
    for item in objects:
        if item["kind"] != "Namespace":
            add_fixtures(item)
            api("create", "-f", "-", doc=item)
    check(
        "installation starts with zero receivers and the scheduled preparer suspended",
        field("deployment", EXECUTOR, "{.spec.replicas}") == "0"
        and field("cronjob", PREPARER, "{.spec.suspend}") == "true",
    )
    wait_job("jaeger-demo-bundle-init")
    check(
        "bootstrap loads the candidate image modules and initializes the bound PVC",
        field("pvc", "jaeger-demo-bundles", "{.status.phase}") == "Bound",
    )
    claim = field("pvc", "jaeger-demo-bundles", "{.metadata.uid}:{.spec.volumeName}")
    ns("scale", "deployment/" + EXECUTOR, "--replicas=1")
    rollout()
    for name, actor in ((PREPARER, principals[1]), (EXECUTOR, principals[2]), ("requester", REQUESTER)):
        denied = subprocess.run(
            [*kube, "auth", "can-i", "get", "secrets", "-n", NS, "--as=" + actor],
            text=True,
            capture_output=True,
        )
        check(
            name + " has no Kubernetes API Secret read grant",
            denied.returncode == 1 and denied.stdout.strip() == "no",
        )
    check(
        "executor reports ready on its independently authenticated service account",
        field("deployment", EXECUTOR, "{.status.readyReplicas}") == "1",
    )
    uid_before = field("deployment", EXECUTOR, "{.metadata.uid}")
    for n, outcome in enumerate(("success", "failure"), 1):
        if n == 2:
            slot += timedelta(days=1)
            ns(
                "patch",
                "configmap",
                "fixture-app",
                "--type=merge",
                "-p",
                json.dumps({"data": {"slot": protocol.format_utc(slot), "outcome": outcome}}),
            )
            ns("rollout", "restart", "deployment/" + EXECUTOR)
            rollout()
            check(
                "replacement executor sees the original verified result without a second apply",
                len(outcome_records()) == 2
                and field("configmap", protocol.REQUEST[1], "{.data.state}") == "succeeded",
            )
        job = "fixture-prepare-" + str(n)
        ns("create", "job", job, "--from=cronjob/" + PREPARER)
        wait_job(job)
        wait_state("ready")
        obj = json.loads(ns("get", "configmap", protocol.REQUEST[1], "-o", "json"))
        stamp = protocol.format_utc(slot)
        signal = [
            {"op": "test", "path": "/metadata/uid", "value": obj["metadata"]["uid"]},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": obj["metadata"]["resourceVersion"],
            },
            {"op": "test", "path": "/data/slot", "value": stamp},
            {"op": "test", "path": "/data/state", "value": "ready"},
            {"op": "replace", "path": "/data/requestedAt", "value": stamp},
            {"op": "replace", "path": "/data/state", "value": "requested"},
        ]
        api(
            "--as=" + REQUESTER,
            "-n",
            NS,
            "patch",
            "configmap",
            protocol.REQUEST[1],
            "--type=json",
            "-p",
            json.dumps(signal),
        )
        state, phase = ("succeeded", "verified") if outcome == "success" else ("failed", "failed")
        wait_state(state)
        receipts = [r for r in outcome_records() if r["slot"] == stamp]
        checkpoint = json.loads(
            api("-n", "default", "get", "configmap", protocol.CHECKPOINT[1], "-o", "json")
        )
        lock = checkpoint["metadata"]["annotations"].get(protocol.HOLDER)
        check(
            "dated "
            + outcome
            + " verifies separate key projections, mount modes and signed encrypted receipt",
            {r["phase"] for r in receipts} == {"started", phase}
            and len(receipts) == 2
            and all(r["bundleSHA256"] == obj["data"]["bundleSHA256"] for r in receipts)
            and lock == (None if outcome == "success" else protocol.daily_owner(slot, obj)),
        )
    check(
        "both workloads kept the same PVC and the CronJob remains suspended",
        field("pvc", "jaeger-demo-bundles", "{.metadata.uid}:{.spec.volumeName}") == claim
        and field("cronjob", PREPARER, "{.spec.suspend}") == "true"
        and field("deployment", EXECUTOR, "{.metadata.uid}") == uid_before,
    )
    ns("scale", "deployment/" + EXECUTOR, "--replicas=0")
    return {
        "image": image,
        "cases": cases,
        "kubernetes": json.loads(api("version", "-o", "json"))["serverVersion"]["gitVersion"],
        "productionAccess": False,
        "applicationAdapter": "fixture-only read-only adapter and fixed configuration",
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kubeconfig", required=True, type=Path)
    p.add_argument("--work", required=True, type=Path)
    p.add_argument("--image", required=True)
    p.add_argument("--receipt", type=Path)
    args = p.parse_args()
    if args.receipt and args.receipt.exists():
        p.error("Supply a new receipt path")
    result = rehearse(
        ["kubectl", "--kubeconfig", str(args.kubeconfig), "--context", CONTEXT], args.image, args.work
    )
    if args.receipt:
        with args.receipt.open("x") as out:
            json.dump(result, out, indent=2)
            out.write("\n")
    print("Passed", len(result["cases"]), "executor workload checks")
