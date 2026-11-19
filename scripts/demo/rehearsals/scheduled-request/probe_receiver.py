#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Run separate preparer and receiver processes against a fresh, loopback-only rehearsal API."""

import argparse
import base64
import contextlib
import json
from ipaddress import ip_address
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

import access
import probe_api
import scheduled_bundle as custody
import scheduled_receiver as receiver
import scheduled_request as protocol


def subject(role):
    return f"system:serviceaccount:{receiver.NAMESPACE}:{receiver.ACCOUNTS[role]}"


REQUESTER = "fixture-receiver-requester"
RECOVERY = "fixture-receiver-recovery"
SHA = "a" * 40


class FixtureAdapter:
    def __init__(self, root):
        self.root = root

    def prepare(self, slot, configuration_sha):
        return {p: (p + slot + configuration_sha).encode() for p in custody.MEMBERS}

    def apply(self, files, *, slot, configuration_sha, assert_locked):
        assert_locked()
        if files != self.prepare(slot, configuration_sha):
            raise AssertionError("Decrypted fixture contents differ")
        with (self.root / "applications").open("a") as trace:
            print(slot, file=trace)
        outcome = (self.root / "outcome").read_text()
        if outcome == "crash-fixture-process":
            os._exit(73)
        return outcome == "success"


def run_worker(op, root, kubectl):
    receiver.KUBECTL = kubectl
    receiver.POLL_SECONDS = 0.1
    receiver.READINESS = root / "readiness"
    paths = receiver.Paths(root / "bundles", root / op / "keys", root / "outcomes")
    server = (root / "loopback-server").read_text()
    with receiver.cluster_config(root / op / "sa", server):
        control = receiver.InClusterControl()
        receiver.authenticate(control, op)

        def now():
            return protocol.timestamp((root / "clock").read_text())

        adapter = FixtureAdapter(root)
        if op == "prepare":
            receiver.prepare_slot(control, custody, adapter, lambda: SHA, paths, now=now)
        else:
            receiver.check_mounts(paths)
            receiver.serve(control, custody, adapter, paths, now=now)


def wait_until(check, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.2)
    raise AssertionError("Timed out waiting for the receiver fixture")


def run_cases(kube, root):
    def api(*args, doc=None):
        return probe_api.run_api(kube, *args, doc=doc)

    server = api("config", "view", "--minify", "-o", "jsonpath={.clusters[0].cluster.server}")
    # Both the kind entry point and standalone etcd/apiserver harness use an authenticated loopback endpoint.
    url = urlsplit(server)
    if url.scheme != "https" or not ip_address(url.hostname).is_loopback or url.username or url.password:
        raise ValueError("Receiver rehearsal requires loopback HTTPS")
    if api("get", "namespace", receiver.NAMESPACE, "--ignore-not-found", "-o", "name").strip():
        raise ValueError("Receiver rehearsal requires a fresh namespace")
    if api(
        "get", "configmap", protocol.CHECKPOINT[1], "-n", "default", "--ignore-not-found", "-o", "name"
    ).strip():
        raise ValueError("Receiver rehearsal requires a fresh checkpoint")
    root.mkdir(mode=0o700)
    (root / "loopback-server").write_text(server)
    (root / "bundles").mkdir(mode=0o700)
    (root / "outcomes").mkdir(mode=0o700)
    (root / "outcome").write_text("success")
    custody.generate_keys(root / "bootstrap-keys")
    local_ca = api(
        "config",
        "view",
        "--minify",
        "--raw",
        "--flatten",
        "-o",
        "jsonpath={.clusters[0].cluster.certificate-authority-data}",
    )
    ca = base64.b64decode(local_ca, validate=True)
    kubectl = shutil.which(str(kube[0]))
    if not kubectl or not ca:
        raise ValueError("Expected a kubectl binary and CA for the fixture")
    api("create", "configmap", protocol.CHECKPOINT[1], "-n", "default", "--from-literal=fixture=fixed")
    api("annotate", "configmap", protocol.CHECKPOINT[1], "-n", "default", f"{protocol.PROTOCOL}=v1")
    resources = list(access.manifests(REQUESTER, subject("prepare"), subject("serve"), RECOVERY))
    for obj in resources:
        if obj["kind"] != "RoleBinding":
            api("create", "-f", "-", doc=obj)
    access.verify_barrier(
        lambda: subprocess.run(
            [*kube, *access.barrier_command()], text=True, capture_output=True, timeout=20
        )
    )
    for obj in resources:
        if obj["kind"] == "RoleBinding":
            api("create", "-f", "-", doc=obj)
    for op, keys in (
        ("prepare", ("signing-key.pem", "verification-key.pem", "recipient-public.pem")),
        ("serve", ("verification-key.pem", "decryption-key.pem")),
    ):
        role_root = root / op
        sa = role_root / "sa"
        sa.mkdir(mode=0o700, parents=True)
        (role_root / "keys").mkdir(mode=0o700)
        for name in keys:
            shutil.copy2(root / "bootstrap-keys" / name, role_root / "keys" / name)
        account = receiver.ACCOUNTS[op]
        api("-n", receiver.NAMESPACE, "create", "serviceaccount", account)
        token = api("-n", receiver.NAMESPACE, "create", "token", account, "--duration=20m").strip()
        (sa / "token").write_text(token)
        (sa / "namespace").write_text(receiver.NAMESPACE)
        (sa / "ca.crt").write_bytes(ca)
    api(
        "-n",
        "default",
        "create",
        "role",
        "receiver-checkpoint",
        "--verb=get,patch",
        "--resource=configmaps",
        "--resource-name=" + protocol.CHECKPOINT[1],
    )
    api(
        "-n",
        "default",
        "create",
        "rolebinding",
        "receiver-checkpoint",
        "--role=receiver-checkpoint",
        "--user=" + RECOVERY,
        "--serviceaccount=" + receiver.NAMESPACE + ":" + receiver.ACCOUNTS["serve"],
    )

    sender = probe_api.FixtureActor(kube, REQUESTER)
    slot = protocol.timestamp("2026-11-19T13:00:00Z")
    cases = []

    def checked(condition, description):
        if not condition:
            raise AssertionError(description)
        cases.append(description)

    def launch(op):
        return subprocess.Popen(
            [sys.executable, __file__, "--worker", op, "--root", str(root), "--kubectl", kubectl],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def prepare():
        with launch("prepare") as p:
            if p.wait(timeout=30) != 0:
                raise AssertionError("Fixture preparer failed")

    @contextlib.contextmanager
    def service():
        (root / "readiness").unlink(missing_ok=True)
        with launch("serve") as p:
            try:
                yield p
            finally:
                p.terminate()
                p.wait(timeout=8)

    def state():
        return sender.read(protocol.REQUEST)["data"]["state"]

    def evidence(phase):
        return list((root / "outcomes").glob(f"*.{phase}.json"))

    receiver.KUBECTL = kubectl
    with receiver.cluster_config(root / "prepare" / "sa", server):
        try:
            receiver.authenticate(receiver.InClusterControl(), "serve")
        except receiver.ReceiverError:
            cases.append("an authentic preparer token cannot start the executor role")
        else:
            raise AssertionError("Unexpected role accepted")
    (root / "clock").write_text(protocol.format_utc(slot))
    prepare()
    initial = sender.read(protocol.REQUEST)
    prepare()
    checked(
        sender.read(protocol.REQUEST) == initial,
        "preparer restart reuses the signed dated receipt without another request write",
    )
    with service() as p:
        wait_until(lambda: (root / "readiness").exists())
        checked(
            p.poll() is None and state() == "ready",
            "separate projected service accounts authenticate and publish readiness",
        )
        protocol.signal(sender, initial, slot)
        wait_until(lambda: state() == "succeeded")
        checked(
            len(evidence("started")) == len(evidence("verified")) == 1,
            "verified encrypted bundle and durable outcome precede GitHub success",
        )
    with service():
        wait_until(lambda: (root / "readiness").exists())
        checked(
            json.loads((root / "readiness").read_bytes())["state"] == "idle" and state() == "succeeded",
            "restarted receiver observes completed slot without reapplying it",
        )
        tomorrow = slot + protocol.timedelta(days=1)
        (root / "clock").write_text(protocol.format_utc(tomorrow))
        prepare()
        (root / "outcome").write_text("failure")
        protocol.signal(sender, sender.read(protocol.REQUEST), tomorrow)
        wait_until(lambda: state() == "failed")
        checked(
            len(evidence("failed")) == 1,
            "failed verification writes durable failure and preserves the claim",
        )
    with service() as p:
        wait_until(lambda: (root / "readiness").exists())
        status = json.loads((root / "readiness").read_bytes())
        checked(
            p.poll() is None and status["state"] == "inspect-failed",
            "restarted receiver reports failed state for operator inspection",
        )
        checked(
            (root / "applications").read_text().splitlines()
            == [protocol.format_utc(slot), protocol.format_utc(tomorrow)],
            "restart after success or failure never replays the application",
        )
        checked(
            api("-n", "default", "get", "configmap", protocol.CHECKPOINT[1], "-o", "json").find(
                "daily/" + protocol.format_utc(tomorrow)
            )
            >= 0,
            "shared writer lock remains held after application failure",
        )
    operator = probe_api.FixtureActor(kube, RECOVERY)
    inspected = tuple(
        (meta["uid"], meta["resourceVersion"])
        for meta in (
            operator.read(protocol.REQUEST)["metadata"],
            operator.read(protocol.CHECKPOINT)["metadata"],
        )
    )
    receipt = custody.trusted_receipt(
        root / "bundles",
        protocol.format_utc(tomorrow),
        verification_key=root / "serve/keys/verification-key.pem",
    )
    protocol.recover(operator, tomorrow, receiver.ENGINE, receipt, inspected, RECOVERY, True)
    third = tomorrow + protocol.timedelta(days=1)
    (root / "clock").write_text(protocol.format_utc(third))
    (root / "outcome").write_text("crash-fixture-process")
    prepare()
    protocol.signal(sender, sender.read(protocol.REQUEST), third)
    with service() as p:
        checked(
            p.wait(timeout=30) == 73 and state() == "running",
            "abrupt process exit retains its running claim",
        )
    with service():
        wait_until(lambda: (root / "readiness").exists())
        checked(
            json.loads((root / "readiness").read_bytes())["state"] == "inspect-running"
            and len(evidence("started")) == 3
            and len(evidence("verified")) == len(evidence("failed")) == 1,
            "replacement receiver preserves the interrupted evidence and never replays an ambiguous operation",
        )
    return {
        "cases": cases,
        "kubernetes": json.loads(api("version", "-o", "json"))["serverVersion"]["gitVersion"],
        "productionAccess": False,
        "applicationAdapter": "synthetic fixed callback",
        "privateCustody": "real signed and encrypted fixture bytes",
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--kubectl", default="kubectl")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--worker", choices=["serve", "prepare"], help=argparse.SUPPRESS)
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        run_worker(args.worker, args.root, args.kubectl)
    else:
        if not args.kubeconfig or not args.receipt or args.receipt.exists():
            parser.error("Supply a dedicated kind kubeconfig and new receipt path")
        with tempfile.TemporaryDirectory() as tmp:
            receipt = run_cases(probe_api.local_kind(args), Path(tmp) / "receiver")
        with args.receipt.open("x") as out:
            json.dump(receipt, out, indent=2)
            out.write("\n")
