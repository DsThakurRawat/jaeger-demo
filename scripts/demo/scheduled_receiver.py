# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Run the independently authenticated daily preparer or receiver inside Kubernetes."""

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time

import scheduled_request as protocol

ENGINE = "scripts-v1"
NAMESPACE = protocol.REQUEST[0]
ACCOUNTS = {"prepare": "jaeger-demo-preparer", "serve": "jaeger-demo-executor"}
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
KUBECTL = "/usr/local/bin/kubectl"
POLL_SECONDS = 10
READINESS = Path("/tmp/jaeger-receiver-ready.json")
WINDOW = timedelta(hours=2)
APPLICATION_BUDGET = protocol.EXECUTION_TIMEOUT - timedelta(minutes=5)


@dataclass(frozen=True)
class Paths:
    store: Path = Path("/var/lib/jaeger-bundles")
    keys: Path = Path("/var/run/jaeger-bundle-keys")
    outcomes: Path = Path("/var/lib/jaeger-outcomes")


class ReceiverError(RuntimeError):
    pass


class InClusterControl(protocol.ConfigMaps):
    def command(self, *args):
        result = subprocess.run(
            [KUBECTL, "--request-timeout=15s", *args], text=True, capture_output=True, timeout=20
        )
        if result.returncode:
            raise ReceiverError("Kubernetes control request failed")
        return result.stdout


@contextmanager
def cluster_config(account=SERVICE_ACCOUNT, server="https://kubernetes.default.svc"):
    """Point kubectl, Helm and the adapter at the same kubelet-rotated projected token."""
    if (account / "namespace").read_text().strip() != NAMESPACE or not all(
        (account / name).is_file() for name in ("token", "ca.crt")
    ):
        raise ReceiverError("The expected namespace and service account projection are required")
    config = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [
            {
                "name": "local",
                "cluster": {"server": server, "certificate-authority": str(account / "ca.crt")},
            }
        ],
        "users": [{"name": "pod", "user": {"tokenFile": str(account / "token")}}],
        "contexts": [
            {"name": "pod", "context": {"cluster": "local", "user": "pod", "namespace": NAMESPACE}}
        ],
        "current-context": "pod",
    }
    with tempfile.TemporaryDirectory(dir="/tmp") as temp:
        target = Path(temp) / "kubeconfig"
        target.write_text(json.dumps(config))
        previous = os.environ.get("KUBECONFIG")
        os.environ["KUBECONFIG"] = str(target)
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("KUBECONFIG", None)
            else:
                os.environ["KUBECONFIG"] = previous


def authenticate(store, operation):
    info = json.loads(store.command("auth", "whoami", "-o", "json"))
    if info["status"]["userInfo"]["username"] != f"system:serviceaccount:{NAMESPACE}:{ACCOUNTS[operation]}":
        raise ReceiverError("The API reported a different service account than the installed role")


def prepare_slot(store, bundle, adapter, read_sha, paths, now=protocol.utcnow):
    instant = now()
    slot = protocol.daily_slot(instant)
    stamp = protocol.format_utc(slot)
    expires = slot + WINDOW
    if not slot - WINDOW <= instant < expires:
        raise ReceiverError("Outside the daily preparation window")
    try:
        receipt = bundle.trusted_receipt(
            paths.store, stamp, verification_key=paths.keys / "verification-key.pem"
        )
    except bundle.SlotNotFound:
        sha = read_sha()
        if not isinstance(sha, str) or not re.fullmatch("[a-f0-9]{40}", sha):
            raise ReceiverError("Expected the reviewed main configuration commit")
        files = adapter.prepare(stamp, sha)
        if now() >= expires:
            raise ReceiverError("Preparation exceeded the daily admission window")
        receipt = bundle.publish(
            paths.store,
            stamp,
            sha,
            files,
            signing_key=paths.keys / "signing-key.pem",
            recipient_key=paths.keys / "recipient-public.pem",
        )
    protocol.prepare(store, slot, expires, ENGINE, protocol.receipt_fields(receipt), now())
    return receipt


def persist_outcome(folder, phase, record):
    """A previously started bundle is never executed a second time, even after a process restart."""
    target = folder / f"{record['slot'][:10]}.{record['bundleSHA256']}.{phase}.json"
    payload = {**record, "phase": phase, "recordedAt": protocol.format_utc(protocol.utcnow())}
    with tempfile.NamedTemporaryFile(dir=folder, mode="w", delete=False) as stream:
        temp = Path(stream.name)
        try:
            json.dump(payload, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.link(temp, target)
            fd = os.open(folder, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temp.unlink()


@contextmanager
def application_deadline():
    # The runtime runs this Python process as the container entrypoint. Exiting
    # tears down its subprocesses and preserves the unconfirmed claim for recovery.
    def expire():
        try:
            os.write(
                2, b"Protected application exceeded its execution budget. Inspect the recorded claim.\n"
            )
        finally:
            os._exit(124)

    deadline = threading.Timer(APPLICATION_BUDGET.total_seconds(), expire)
    deadline.daemon = True
    deadline.start()
    try:
        yield
    finally:
        deadline.cancel()


def poll(store, bundle, adapter, paths, now=protocol.utcnow, active=lambda: None):
    obj = store.read(protocol.REQUEST)
    if not obj:
        raise ReceiverError("The protected control record is missing")
    data = obj["data"]
    if data == {"schema": protocol.SCHEMA, "state": "empty"}:
        return "idle"
    # Ignore the old slot after completion. A live claim from any date is never adopted.
    slot = protocol.timestamp(data["slot"])
    data = protocol.validate(obj, slot, ENGINE, data["configurationSHA"])
    if data["state"] in {"running", "verified", "failed"}:
        return "inspect-" + data["state"]
    if data["state"] != "requested":
        return "idle"
    instant = now()
    if slot != protocol.daily_slot(instant) or not protocol.timestamp(data["requestedAt"]) <= instant < min(
        protocol.timestamp(data["expiresAt"]),
        protocol.timestamp(data["requestedAt"]) + protocol.ACK_TIMEOUT,
    ):
        return "expired-unclaimed"

    stamp = protocol.format_utc(slot)
    receipt = protocol.receipt_fields(
        bundle.trusted_receipt(paths.store, stamp, verification_key=paths.keys / "verification-key.pem")
    )
    sha = receipt["configurationSHA"]
    claim = dict(data, state="running")
    uid = obj["metadata"]["uid"]
    expected_owner = protocol.daily_owner(slot, obj)

    def apply(frozen):
        deadline = time.monotonic() + APPLICATION_BUDGET.total_seconds()

        def assert_locked():
            live = store.read(protocol.REQUEST)
            checkpoint = store.read(protocol.CHECKPOINT)
            annotations = (checkpoint or {}).get("metadata", {}).get("annotations", {})
            if (
                time.monotonic() >= deadline
                or not live
                or live["metadata"]["uid"] != uid
                or live.get("data") != claim
                or annotations.get(protocol.PROTOCOL) != "v1"
                or annotations.get(protocol.HOLDER) != expected_owner
            ):
                raise ReceiverError("The original operation no longer holds an eligible deployment claim")

        assert_locked()
        active()
        record = {
            "schema": "jaeger-demo/receiver-outcome/v1",
            "slot": stamp,
            "engine": ENGINE,
            "requestUID": uid,
            **frozen,
        }
        persist_outcome(paths.outcomes, "started", record)
        try:
            with application_deadline():
                files = bundle.load(
                    paths.store,
                    stamp,
                    frozen,
                    verification_key=paths.keys / "verification-key.pem",
                    decryption_key=paths.keys / "decryption-key.pem",
                )
                assert_locked()
                if (
                    adapter.apply(files, slot=stamp, configuration_sha=sha, assert_locked=assert_locked)
                    is not True
                ):
                    raise ReceiverError("The fixed application adapter did not verify all five components")
                assert_locked()
                persist_outcome(paths.outcomes, "verified", record)
        except Exception:
            persist_outcome(paths.outcomes, "failed", record)
            raise ReceiverError("The daily application or verification failed") from None
        return True

    protocol.execute(store, slot, sha, receipt, apply, now=now, engine=ENGINE)
    return "succeeded"


def mark_ready(state):
    with tempfile.NamedTemporaryFile(mode="w", dir=READINESS.parent, delete=False) as fp:
        json.dump({"state": state, "checkedAt": protocol.format_utc(protocol.utcnow())}, fp)
        name = fp.name
    os.replace(name, READINESS)


def ready():
    if not READINESS.is_file():
        return False
    row = json.loads(READINESS.read_bytes())
    age = protocol.utcnow() - protocol.timestamp(row["checkedAt"])
    limit = protocol.EXECUTION_TIMEOUT if row["state"] == "applying" else timedelta(seconds=45)
    return timedelta(0) <= age < limit


def serve(store, bundle, adapter, paths, now=protocol.utcnow):
    previous = None
    while True:
        try:
            state = poll(store, bundle, adapter, paths, now=now, active=lambda: mark_ready("applying"))
            mark_ready(state)
        except Exception as error:
            # Subprocess errors and bundle/Helm diagnostics can contain private input.
            state = "poll-error/" + type(error).__name__
            READINESS.unlink(missing_ok=True)
        if state != previous:
            print(json.dumps({"receiver": state, "at": protocol.format_utc(protocol.utcnow())}), flush=True)
        previous = state
        time.sleep(POLL_SECONDS)


def check_mounts(paths):
    if not paths.store.is_dir() or not all(
        (paths.keys / name).is_file() for name in ("verification-key.pem", "decryption-key.pem")
    ):
        raise ReceiverError("The bundle store and receiver key mounts are required")
    # Exercise the writable outcome mount before the first request claims a production lock.
    with tempfile.TemporaryFile(dir=paths.outcomes) as file:
        file.write(b"probe")
        file.flush()
        os.fsync(file.fileno())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["prepare", "serve", "ready"])
    op = parser.parse_args().operation
    if op == "ready":
        return 0 if ready() else 1
    if op == "serve":
        READINESS.unlink(missing_ok=True)
    import scheduled_adapter as adapter
    import scheduled_bundle as bundle

    with cluster_config():
        store = InClusterControl()
        authenticate(store, op)
        paths = Paths()
        if op == "prepare":
            import scheduled_inputs

            receipt = prepare_slot(store, bundle, adapter, scheduled_inputs.main_sha, paths)
            print(json.dumps({"prepared": receipt}, sort_keys=True))
            return 0
        check_mounts(paths)
        serve(store, bundle, adapter, paths)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as error:
        print("Scheduled receiver stopped: " + type(error).__name__, file=sys.stderr)
        code = 1
    sys.exit(code)
