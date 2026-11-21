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
STATES = {"ready", "requested", "running", "verified", "succeeded", "failed", "recovered"}
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


def unprepared(slot, recorded):
    """Name the cause when the request record does not belong to the expected slot.

    The preparer replaces the record once per slot. A record from another slot therefore
    means that no preparation was published for this one, whatever stopped it. Only a
    well-formed timestamp from the record is repeated in the message.
    """
    message = "No preparation was published for the " + format_utc(slot) + " slot"
    if isinstance(recorded, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", recorded):
        return message + ": the request record still holds " + recorded
    return message + ": the request record names no valid slot"


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
    if data == {"schema": SCHEMA, "state": "empty"}:
        raise RequestError("No preparation has been published: the request record is still empty")
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
    if slot == daily_slot(slot) and data["slot"] != format_utc(slot):
        raise RequestError(unprepared(slot, data["slot"]))
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


def preparation_change(store, slot, expires, engine, receipt, now):
    """Validate the intended transition without publishing it or modifying the control record."""
    data = {**description(slot, expires, engine, receipt), "state": "ready", "requestedAt": ""}
    if now >= expires:
        raise RequestError("Refusing to prepare an expired daily slot")
    old = store.read(REQUEST)
    if old is None:
        raise RequestError("The operator must bootstrap the named request record and its admission policy")
    if old.get("data") == {"schema": SCHEMA, "state": "empty"}:
        return old, data
    if old.get("data") != {
        **data,
        "state": old.get("data", {}).get("state"),
        "requestedAt": old.get("data", {}).get("requestedAt"),
    }:
        prior = old["data"]
        validate(old, timestamp(prior["slot"]), prior["engine"], prior["configurationSHA"])
        if timestamp(prior["slot"]) >= slot or prior["state"] in {"running", "verified", "failed"}:
            raise RequestError("An existing slot is newer, conflicting, running or awaiting recovery")
        if prior["state"] not in {"succeeded", "recovered"} and now < timestamp(prior["expiresAt"]):
            raise RequestError("The previous slot is still live")
        return old, data
    validate(old, slot, engine, receipt["configurationSHA"])
    return None


def prepare(store, slot, expires, engine, receipt, now):
    change = preparation_change(store, slot, expires, engine, receipt, now)
    if change:
        old, data = change
        store.patch(REQUEST, old, [{"op": "replace", "path": "/data", "value": data}])


def request_and_wait(store, slot, sha, now=utcnow, sleep=time.sleep):
    """No cluster writes other than the CAS from ready to requested."""
    obj = store.read(REQUEST)
    data = validate(obj, slot, "scripts-v1", sha)
    frozen = {k: v for k, v in data.items() if k not in {"state", "requestedAt"}}
    signalled = data["requestedAt"]
    uid = obj["metadata"]["uid"]
    start = now()
    if data["state"] == "ready":
        if start < slot or start >= timestamp(data["expiresAt"]):
            raise RequestError("The original scheduled run is outside its reviewed request window")
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
        if state == "recovered":
            raise RequestError("The protected executor reported failure and an operator reconciled it")
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
    release_observed(store, obj, owner)


def release_observed(store, obj, owner):
    path = "/metadata/annotations/" + pointer(HOLDER)
    store.patch(
        CHECKPOINT, obj, [{"op": "test", "path": path, "value": owner}, {"op": "remove", "path": path}]
    )


def daily_owner(slot, obj):
    return "daily/" + format_utc(slot) + "/" + obj["metadata"]["uid"]


def recover(store, slot, engine, receipt, reviewed, operator, confirmed):
    """Reconcile only the reviewed failed/verified operation, without running its bundle again."""
    if not confirmed:
        raise RequestError(
            "Confirm the owner stopped and the live outcome and durable evidence were verified"
        )
    identity = json.loads(store.command("auth", "whoami", "-o", "json"))
    if identity["status"]["userInfo"]["username"] != operator:
        raise RequestError("The authenticated recovery operator differs from the reviewed identity")
    receipt = receipt_fields(receipt)
    obj = store.read(REQUEST)
    data = validate(obj, slot, engine, receipt["configurationSHA"])
    if data["bundleSHA256"] != receipt["bundleSHA256"] or data["state"] not in {"failed", "verified"}:
        raise RequestError("Only the reviewed failed or durably verified operation can be reconciled")
    checkpoint = store.read(CHECKPOINT)
    if (
        not checkpoint
        or tuple(
            (record["metadata"]["uid"], record["metadata"]["resourceVersion"])
            for record in (obj, checkpoint)
        )
        != reviewed
    ):
        raise RequestError("The request or checkpoint changed since the operator's inspection")

    owner = daily_owner(slot, obj)
    annotations = checkpoint["metadata"].get("annotations", {})
    if annotations.get(PROTOCOL) != "v1" or annotations.get(HOLDER) not in {None, owner}:
        raise RequestError("The checkpoint protocol or lock owner differs from the reviewed operation")
    if HOLDER in annotations:
        release_observed(store, checkpoint, owner)
    # If this CAS fails after release, re-inspect both records. An absent lock after
    # a verified stop is safe to resume. A foreign owner is never cleared.
    set_state(store, obj, "recovered" if data["state"] == "failed" else "succeeded")


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
    owner = daily_owner(slot, obj)
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


def recovery_main():
    parser = argparse.ArgumentParser(description=recover.__doc__)
    parser.add_argument("--slot", required=True, type=timestamp)
    parser.add_argument("--engine", choices=sorted(ENGINES), required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--operator", required=True, help="Reviewed Kubernetes recovery username")
    for record in ("request", "checkpoint"):
        parser.add_argument("--" + record + "-uid", required=True)
        parser.add_argument("--" + record + "-version", required=True)
    parser.add_argument("--confirmed-stopped-and-reconciled", action="store_true", required=True)
    args = parser.parse_args(sys.argv[2:])
    recover(
        ConfigMaps(),
        args.slot,
        args.engine,
        json.loads(args.receipt.read_bytes()),
        ((args.request_uid, args.request_version), (args.checkpoint_uid, args.checkpoint_version)),
        args.operator,
        args.confirmed_stopped_and_reconciled,
    )


def main():
    if sys.argv[1:2] == ["recover"]:
        recovery_main()
        return
    parser = argparse.ArgumentParser(
        description=__doc__, epilog="For operator recovery, run recover --help."
    )
    parser.add_argument("operation", choices=["request", "manual-acquire", "manual-release"])
    args = parser.parse_args()
    store = ConfigMaps()
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
