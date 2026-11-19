# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""The scheduled credential can signal one fixed request and observe its verified result."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

REQUEST = ("jaeger-demo-operations", "daily-hosted-update")
CHECKPOINT = ("default", "jaeger-demo-deployment-revisions")
PROTOCOL = "jaegertracing.io/deployment-lock-version"
HOLDER = "jaegertracing.io/deployment-lock-holder"
SCHEMA = "jaeger-demo/daily-request/v1"
ENGINES = {"scripts-v1", "helmfile-v1"}
STATES = {"ready", "requested", "running", "verified", "succeeded", "failed"}
ACK_TIMEOUT = timedelta(minutes=5)
MAX_LIFETIME = timedelta(hours=6)
EXECUTION_TIMEOUT = timedelta(minutes=280)


class RequestError(RuntimeError):
    pass


def utcnow():
    return datetime.now(timezone.utc)


def timestamp(text):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", text):
        raise RequestError("Expected a UTC timestamp in YYYY-MM-DDTHH:MM:SSZ format")
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def format_utc(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def daily_slot(run_created):
    return run_created.astimezone(timezone.utc).replace(hour=13, minute=0, second=0, microsecond=0)


def receipt_fields(receipt):
    if (
        set(receipt) != {"configurationSHA", "bundleSHA256"}
        or not isinstance(receipt["configurationSHA"], str)
        or not re.fullmatch("[0-9a-f]{40}", receipt["configurationSHA"])
        or not isinstance(receipt["bundleSHA256"], str)
        or not re.fullmatch("[0-9a-f]{64}", receipt["bundleSHA256"])
    ):
        raise RequestError("Invalid immutable bundle receipt")
    return dict(receipt)


def description(slot, expires, engine, receipt):
    if engine not in ENGINES or slot != daily_slot(slot) or not slot < expires <= slot + MAX_LIFETIME:
        raise RequestError("Invalid reviewed execution engine or schedule interval")
    return {
        **receipt_fields(receipt),
        "schema": SCHEMA,
        "slot": format_utc(slot),
        "expiresAt": format_utc(expires),
        "engine": engine,
    }


def validate(obj, slot, engine, sha):
    if not obj:
        raise RequestError("No protected receiver request exists for the daily update")
    meta = obj.get("metadata", {})
    if (
        obj.get("apiVersion") != "v1"
        or obj.get("kind") != "ConfigMap"
        or (meta.get("namespace"), meta.get("name")) != REQUEST
        or not meta.get("uid")
        or not meta.get("resourceVersion")
    ):
        raise RequestError("Unexpected Kubernetes request identity")
    data = obj.get("data", {})
    if (
        set(data)
        != {
            "schema",
            "slot",
            "expiresAt",
            "engine",
            "configurationSHA",
            "bundleSHA256",
            "state",
            "requestedAt",
        }
        or data["state"] not in STATES
    ):
        raise RequestError("Malformed protected receiver request")
    expected = description(
        slot,
        timestamp(data["expiresAt"]),
        engine,
        {"configurationSHA": sha, "bundleSHA256": data["bundleSHA256"]},
    )
    signalled = data["requestedAt"]
    if data["state"] == "ready":
        if signalled != "":
            raise RequestError("Unrequested slot has a signal time")
    elif not slot <= timestamp(signalled) < timestamp(data["expiresAt"]):
        raise RequestError("Signal time outside the reviewed request window")
    if data != {**expected, "state": data["state"], "requestedAt": signalled}:
        raise RequestError(
            "Prepared request differs from the expected slot, engine or reviewed configuration"
        )
    return data


def pointer(key):
    return key.replace("~", "~0").replace("/", "~1")


class ConfigMaps:
    def command(self, *parts):
        result = subprocess.run(
            ["kubectl", "--request-timeout=20s", *parts], text=True, capture_output=True, timeout=30
        )
        if result.returncode:
            # Kubeconfig, credentials, object contents and server diagnostic bodies stay out of CI logs.
            raise RequestError(f"Kubernetes {parts[2]} failed for the deployment control record")
        return result.stdout

    def read(self, ref):
        namespace, name = ref
        result = self.command("-n", namespace, "get", "configmap", name, "--ignore-not-found", "-o", "json")
        return json.loads(result) if result.strip() else None

    def patch(self, ref, obj, operations):
        namespace, name = ref
        version = obj["metadata"]["resourceVersion"]
        uid = obj["metadata"]["uid"]
        guards = [
            {"op": "test", "path": "/metadata/uid", "value": uid},
            {"op": "test", "path": "/metadata/resourceVersion", "value": version},
        ]
        self.command(
            "-n",
            namespace,
            "patch",
            "configmap",
            name,
            "--type=json",
            "-p",
            json.dumps(guards + operations),
        )


def set_state(store, obj, value):
    store.patch(
        REQUEST,
        obj,
        [
            {"op": "test", "path": "/data/state", "value": obj["data"]["state"]},
            {"op": "replace", "path": "/data/state", "value": value},
        ],
    )


def signal(store, obj, start):
    store.patch(
        REQUEST,
        obj,
        [
            {"op": "test", "path": "/data/state", "value": "ready"},
            {"op": "replace", "path": "/data/requestedAt", "value": format_utc(start)},
            {"op": "replace", "path": "/data/state", "value": "requested"},
        ],
    )


def prepare(store, slot, expires, engine, receipt, now):
    """Called by an independently authorized preparer after it freezes the day's bundle."""
    data = {**description(slot, expires, engine, receipt), "state": "ready", "requestedAt": ""}
    if now >= expires:
        raise RequestError("Refusing to prepare an expired daily slot")
    old = store.read(REQUEST)
    if old is None:
        raise RequestError("The operator must bootstrap the named request record and its admission policy")
    if old.get("data") == {"schema": SCHEMA, "state": "empty"}:
        store.patch(REQUEST, old, [{"op": "replace", "path": "/data", "value": data}])
    elif old.get("data") != {
        **data,
        "state": old.get("data", {}).get("state"),
        "requestedAt": old.get("data", {}).get("requestedAt"),
    }:
        prior = old["data"]
        validate(old, timestamp(prior["slot"]), prior["engine"], prior["configurationSHA"])
        if timestamp(prior["slot"]) >= slot or prior["state"] in {"running", "verified", "failed"}:
            raise RequestError("An existing slot is newer, conflicting, running or awaiting recovery")
        if prior["state"] != "succeeded" and now < timestamp(prior["expiresAt"]):
            raise RequestError("The previous slot is still live")
        store.patch(REQUEST, old, [{"op": "replace", "path": "/data", "value": data}])
    else:
        validate(old, slot, engine, receipt["configurationSHA"])


def request_and_wait(store, slot, sha, now=utcnow, sleep=time.sleep):
    """No cluster writes other than the CAS from ready to requested."""
    obj = store.read(REQUEST)
    data = validate(obj, slot, "scripts-v1", sha)
    frozen = {k: v for k, v in data.items() if k not in {"state", "requestedAt"}}
    signalled = data["requestedAt"]
    uid = obj["metadata"]["uid"]
    start = now()
    if start < slot or start >= timestamp(data["expiresAt"]):
        raise RequestError("The original scheduled run is outside its reviewed request window")
    if data["state"] == "ready":
        try:
            signal(store, obj, start)
        except RequestError:
            # A retry may observe that a concurrent sender (or a lost response) succeeded.
            observed = store.read(REQUEST)
            observed_data = validate(observed, slot, "scripts-v1", sha)
            if observed["metadata"]["uid"] != uid or observed_data["state"] == "ready":
                raise
    while True:
        current = store.read(REQUEST)
        data = validate(current, slot, "scripts-v1", sha)
        if current["metadata"]["uid"] != uid or data != {
            **frozen,
            "state": data["state"],
            "requestedAt": data["requestedAt"],
        }:
            raise RequestError("The daily request changed during execution")
        if signalled and data["requestedAt"] != signalled:
            raise RequestError("The original request timestamp changed")
        signalled = data["requestedAt"]
        state = data["state"]
        if state == "succeeded":
            return receipt_fields({key: data[key] for key in ("configurationSHA", "bundleSHA256")})
        if state == "failed":
            raise RequestError("The protected executor reported failure; inspect its operation record")
        if state == "ready":
            raise RequestError("The protected request returned to ready unexpectedly")
        elapsed = now() - start
        if state == "requested" and (
            now() >= timestamp(signalled) + ACK_TIMEOUT or now() >= timestamp(data["expiresAt"])
        ):
            raise RequestError("The protected executor did not claim the request in time")
        if elapsed >= EXECUTION_TIMEOUT:
            raise RequestError("Timed out waiting for the protected executor's verified result")
        sleep(10)


def acquire(store, owner, required=True):
    """The lock survives a cancelled GitHub run and has no automatic stale takeover."""
    obj = store.read(CHECKPOINT)
    annotations = (obj or {}).get("metadata", {}).get("annotations", {})
    if PROTOCOL not in annotations and HOLDER not in annotations and not required:
        return False
    if annotations.get(PROTOCOL) != "v1":
        raise RequestError("The shared production writer lock has not been provisioned")
    if HOLDER in annotations:
        raise RequestError("The production writer lock is held; inspect the recorded owner before recovery")
    store.patch(
        CHECKPOINT, obj, [{"op": "add", "path": "/metadata/annotations/" + pointer(HOLDER), "value": owner}]
    )
    return True


def release(store, owner):
    obj = store.read(CHECKPOINT)
    if not obj or obj["metadata"].get("annotations", {}).get(HOLDER) != owner:
        raise RequestError("The current process does not own the production writer lock")
    path = "/metadata/annotations/" + pointer(HOLDER)
    store.patch(
        CHECKPOINT, obj, [{"op": "test", "path": path, "value": owner}, {"op": "remove", "path": path}]
    )


def execute(store, slot, sha, reviewed_receipt, verified_apply, now=utcnow, engine="scripts-v1"):
    """Receiver-only integration point. verified_apply lives in the independent executor.

    It must authenticate/decrypt the digest-addressed bundle, recheck live drift, apply only its
    frozen inputs, verify health and durably save its outcome. No command comes from the request.
    """
    expected = receipt_fields(reviewed_receipt)

    def read_requested():
        obj = store.read(REQUEST)
        data = validate(obj, slot, engine, sha)
        if (
            data["state"] != "requested"
            or not timestamp(data["requestedAt"])
            <= now()
            < min(timestamp(data["expiresAt"]), timestamp(data["requestedAt"]) + ACK_TIMEOUT)
            or any(data[k] != v for k, v in expected.items())
        ):
            raise RequestError("Request is stale, already claimed or differs from the independent plan")
        return obj

    obj = read_requested()
    owner = "daily/" + format_utc(slot) + "/" + obj["metadata"]["uid"]
    acquire(store, owner)
    # After this point an ambiguous write leaves the lock held for operator recovery.
    current = read_requested()
    if current["metadata"]["uid"] != obj["metadata"]["uid"] or current["data"] != obj["data"]:
        raise RequestError("Request changed while the protected executor acquired the writer lock")
    set_state(store, current, "running")

    def finish(from_state, state):
        current = store.read(REQUEST)
        if current["metadata"]["uid"] != obj["metadata"]["uid"] or current["data"] != {
            **obj["data"],
            "state": from_state,
        }:
            raise RequestError("The running request no longer matches the executor claim")
        set_state(store, current, state)

    try:
        if verified_apply(dict(expected)) is not True:
            raise RequestError("The executor did not verify the deployment outcome")
    except Exception:
        finish("running", "failed")
        raise
    finish("running", "verified")
    release(store, owner)
    finish("verified", "succeeded")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["request", "manual-acquire", "manual-release", "prepare"])
    parser.add_argument(
        "--receipt", type=Path, help="Independent preparer’s verified public bundle receipt"
    )
    parser.add_argument("--engine", choices=sorted(ENGINES), default="scripts-v1")
    parser.add_argument("--slot", help="UTC date and time of the approved daily slot")
    args = parser.parse_args()
    store = ConfigMaps()
    if args.operation == "prepare":
        if not args.receipt:
            raise RequestError("The protected preparer requires a verified bundle receipt")
        now = utcnow()
        slot = timestamp(args.slot) if args.slot else daily_slot(now)
        prepare(
            store, slot, slot + timedelta(hours=2), args.engine, json.loads(args.receipt.read_bytes()), now
        )
        return
    if args.operation == "request":
        if (
            os.environ.get("GITHUB_EVENT_NAME") != "schedule"
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
        ):
            raise RequestError("Daily requests require a scheduled run of main")
        slot = daily_slot(timestamp(os.environ["SCHEDULE_RUN_CREATED_AT"]))
        receipt = request_and_wait(store, slot, os.environ["GITHUB_SHA"])
        print("Protected executor verified bundle " + receipt["bundleSHA256"])
        return
    run = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ["GITHUB_RUN_ATTEMPT"]
    if not all(re.fullmatch("[1-9][0-9]*", n) for n in (run, attempt)):
        raise RequestError("Malformed GitHub run identity")
    owner = "manual/" + run + "/" + attempt
    if args.operation == "manual-acquire":
        enabled = os.environ.get("DEMO_SCHEDULE_ENABLED") == "true"
        held = acquire(store, owner, required=enabled)
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as out:
            out.write("held=" + str(held).lower() + "\n")
    else:
        release(store, owner)


if __name__ == "__main__":
    try:
        main()
    except (RequestError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        sys.exit(str(error))
