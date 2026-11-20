# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Verify the daily request protocol against a disposable Kubernetes API."""

import argparse
import base64
import contextlib
import ipaddress
import json
from pathlib import Path
import subprocess
from urllib.parse import urlsplit

import yaml

import access
import scheduled_request as request

USERS = [
    "fixture-scheduled-requester",
    "fixture-trusted-preparer",
    "fixture-trusted-executor",
    "fixture-recovery",
]
MANUAL = "fixture-manual"
CONTEXT = "kind-jaeger-request-rehearsal"


def run_api(kube, *parts, doc=None):
    process = subprocess.run(
        [*kube, *parts],
        input=None if doc is None else json.dumps(doc),
        text=True,
        capture_output=True,
        timeout=30,
    )
    if process.returncode:
        raise request.RequestError(process.stderr[:1800])
    return process.stdout


class FixtureActor(request.ConfigMaps):
    def __init__(self, kube, user):
        self.kube = [*kube, "--as", user, "--as-group", "system:authenticated"]

    def command(self, *parts):
        return run_api(self.kube, *parts)


def run_cases(kube):
    version = json.loads(run_api(kube, "version", "-o", "json"))["serverVersion"]["gitVersion"]
    if not version.startswith("v1.36."):
        raise RuntimeError("Use a fresh Kubernetes 1.36.x fixture")

    def create(doc):
        run_api(kube, "create", "-f", "-", doc=doc)

    for group, name in (("namespace", request.REQUEST[0]), ("validatingadmissionpolicy", access.POLICY)):
        if run_api(kube, "get", group, name, "--ignore-not-found", "-o", "name").strip():
            raise RuntimeError("Fixture namespace or admission policy already exists")
    if run_api(
        kube, "-n", "default", "get", "configmap", request.CHECKPOINT[1], "--ignore-not-found", "-o", "name"
    ).strip():
        raise RuntimeError("Fixture checkpoint already exists")
    create(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": request.CHECKPOINT[1],
                "namespace": "default",
                "annotations": {request.PROTOCOL: "v1"},
            },
            "data": {"fixture": "fixed"},
        }
    )
    resources = list(access.manifests(*USERS))
    for obj in resources:
        if obj["kind"] != "RoleBinding":
            create(obj)
    # Native admission watches can lag creation. Keep requester RBAC absent until a
    # real dry-run made with the independent bootstrap identity is denied by this policy.
    access.verify_barrier(
        lambda: subprocess.run(
            [*kube, *access.barrier_command()], capture_output=True, text=True, timeout=30
        )
    )
    for obj in resources:
        if obj["kind"] == "RoleBinding":
            create(obj)
    for user in (USERS[2], USERS[3], MANUAL):
        name = "lock-" + user
        create(
            {
                "apiVersion": access.GROUP + "/v1",
                "kind": "Role",
                "metadata": {"name": name, "namespace": "default"},
                "rules": [
                    {
                        "apiGroups": [""],
                        "resources": ["configmaps"],
                        "verbs": ["get", "patch"],
                        "resourceNames": [request.CHECKPOINT[1]],
                    }
                ],
            }
        )
        create(
            {
                "apiVersion": access.GROUP + "/v1",
                "kind": "RoleBinding",
                "metadata": {"name": name, "namespace": "default"},
                "roleRef": {"apiGroup": access.GROUP, "kind": "Role", "name": name},
                "subjects": [{"kind": "User", "apiGroup": access.GROUP, "name": user}],
            }
        )
    cases = ["guard denied bootstrap probe before requester binding"]

    @contextlib.contextmanager
    def check(label, denial=None):
        try:
            yield
        except request.RequestError as error:
            if denial is None or denial not in str(error):
                raise
        else:
            if denial:
                raise AssertionError(label + " unexpectedly allowed")
        cases.append(label)

    sender, preparer, executor, recovery, manual = [FixtureActor(kube, u) for u in [*USERS, MANUAL]]
    slot = request.timestamp("2026-11-18T13:00:00Z")
    expires = slot + request.timedelta(hours=2)
    receipt = {"configurationSHA": "a" * 40, "bundleSHA256": "b" * 64}
    sha = receipt["configurationSHA"]
    with check("preparer rolls out the first daily slot"):
        request.prepare(preparer, slot, expires, "scripts-v1", receipt, slot)
    original = sender.read(request.REQUEST)
    for label, actor, changes, denial in [
        (
            "requester cannot change configuration",
            sender,
            {"configurationSHA": "d" * 40},
            "Only the independent preparer",
        ),
        (
            "requester cannot change the bundle",
            sender,
            {"bundleSHA256": "e" * 64},
            "Only the independent preparer",
        ),
        (
            "requester cannot claim execution",
            sender,
            {"state": "running", "requestedAt": request.format_utc(slot)},
            "cannot perform",
        ),
        (
            "preparer cannot claim execution",
            preparer,
            {"state": "running", "requestedAt": request.format_utc(slot)},
            "cannot perform",
        ),
        (
            "executor cannot change the bundle",
            executor,
            {"bundleSHA256": "f" * 64},
            "Only the independent preparer",
        ),
    ]:
        with check(label, denial):
            actor.patch(
                request.REQUEST,
                original,
                [{"op": "replace", "path": "/data/" + k, "value": v} for k, v in changes.items()],
            )
    meta_patch = ["--type=merge", "-p", '{"metadata":{"labels":{"fixture":"test"}}}', "--dry-run=server"]
    for label, argv in [
        ("read Helm Secret", ["-n", "default", "get", "secret", "sh.helm.release.v1.prometheus.v7"]),
        ("list Secrets", ["-n", "default", "get", "secrets"]),
        ("read checkpoint", ["-n", "default", "get", "configmap", request.CHECKPOINT[1]]),
        (
            "patch monitoring workload",
            ["-n", "default", "patch", "deployment", "prometheus-grafana", *meta_patch],
        ),
        ("patch Role", ["-n", request.REQUEST[0], "patch", "role", access.ROLE, *meta_patch]),
        ("create request", ["-n", request.REQUEST[0], "create", "configmap", "extra", "--dry-run=server"]),
        (
            "patch monitoring webhook",
            [
                "patch",
                "validatingwebhookconfiguration",
                "prometheus-kube-prometheus-admission",
                *meta_patch,
            ],
        ),
        ("patch admission policy", ["patch", "validatingadmissionpolicy", access.POLICY, *meta_patch]),
    ]:
        with check("requester cannot " + label, "forbidden"):
            sender.command(*argv)
    with check("authenticated requester identity"):
        identity = json.loads(sender.command("auth", "whoami", "-o", "json"))
        if identity["status"]["userInfo"]["username"] != USERS[0]:
            raise AssertionError("Unexpected fixture identity")

    def verified(digest):
        if digest != receipt:
            raise AssertionError("Different plan reached executor")
        with check("manual work cannot overlap receiver health verification", "held"):
            request.acquire(manual, "manual/1/1", False)
        return True

    def receive(_):
        with check("manual writer can acquire before receiver"):
            request.acquire(manual, "manual/2/1", False)
        with check("receiver cannot overlap an existing manual writer", "held"):
            request.execute(executor, slot, sha, receipt, verified, lambda: slot)
        with check("receiver cannot release a different writer's lock", "own"):
            request.release(executor, "different")
        request.release(manual, "manual/2/1")
        request.execute(executor, slot, sha, receipt, verified, lambda: slot)

    with check("daily request waits for verified receiver outcome"):
        if request.request_and_wait(sender, slot, sha, lambda: slot, receive) != receipt:
            raise AssertionError("Unexpected success receipt")

    def unexpected(_):
        raise AssertionError("Duplicate executor invocation")

    with check("replay of completed request reports original success"):
        request.request_and_wait(sender, slot, sha, lambda: slot, unexpected)
    with check("receiver cannot claim completed slot twice", "already claimed"):
        request.execute(executor, slot, sha, receipt, verified, lambda: slot)
    tomorrow = slot + request.timedelta(days=1)
    with check("preparer rolls forward to the next date"):
        request.prepare(
            preparer, tomorrow, expires + request.timedelta(days=1), "scripts-v1", receipt, tomorrow
        )
    request.signal(sender, sender.read(request.REQUEST), tomorrow)
    with check("receiver rejects a late claim", "stale"):
        request.execute(executor, tomorrow, sha, receipt, verified, lambda: tomorrow + request.ACK_TIMEOUT)
    with check("failed verification records failure", "verify"):
        request.execute(executor, tomorrow, sha, receipt, lambda _: False, lambda: tomorrow)
    with check("failed verification retains the writer lock", "held"):
        request.acquire(manual, "manual/3/1", False)
    with check("preparer cannot replace an unresolved failure", "recovery"):
        later = tomorrow + request.timedelta(days=1)
        request.prepare(preparer, later, later + request.timedelta(hours=2), "scripts-v1", receipt, later)
    failed = recovery.read(request.REQUEST)
    checkpoint = recovery.read(request.CHECKPOINT)

    def snapshot(*objects):
        return tuple((o["metadata"]["uid"], o["metadata"]["resourceVersion"]) for o in objects)

    reviewed = snapshot(failed, checkpoint)

    def recover(refs=reviewed, operator=USERS[3], confirmed=True):
        request.recover(recovery, tomorrow, "scripts-v1", receipt, refs, operator, confirmed)

    for actor, name in ((sender, "requester"), (preparer, "preparer"), (executor, "executor")):
        with check(name + " cannot reconcile failed request", "cannot perform"):
            request.set_state(actor, failed, "recovered")
    with check("recovery cannot mark failure successful while its lock is held", "denied request"):
        request.set_state(recovery, failed, "succeeded")
    with check(
        "recovery cannot finalize with the lock still held", "after the checkpoint has been unlocked"
    ):
        request.set_state(recovery, failed, "recovered")
    lock_path = "/metadata/annotations/" + request.pointer(request.HOLDER)
    with check("recovery cannot alter checkpoint revisions while releasing", "only remove the matching"):
        recovery.patch(
            request.CHECKPOINT,
            checkpoint,
            [
                {"op": "remove", "path": lock_path},
                {"op": "replace", "path": "/data/fixture", "value": "changed"},
            ],
        )
    with check("recovery requires explicit verification of the stopped process and outcome", "Confirm"):
        recover(confirmed=False)
    with check("recovery requires the reviewed live username", "identity"):
        recover(operator=USERS[0])
    with check("recovery rejects a stale checkpoint version", "changed since"):
        recover((reviewed[0], (reviewed[1][0], "1")))
    with check("recovery rejects a different request UID", "changed since"):
        recover((("another-uid", reviewed[0][1]), reviewed[1]))
    with check("recovery removes matching lock and records a recovered failure"):
        recover()
        if (
            recovery.read(request.REQUEST)["data"]["state"] != "recovered"
            or request.HOLDER in recovery.read(request.CHECKPOINT)["metadata"]["annotations"]
        ):
            raise AssertionError("Recovery did not complete")
    with check("recovery cannot turn a reconciled failure into success", "cannot perform"):
        request.set_state(recovery, recovery.read(request.REQUEST), "succeeded")
    with check("replay after recovery still reports the original failure", "reported failure"):
        request.request_and_wait(sender, tomorrow, sha, lambda: later, unexpected)
    with check("preparer advances to the next day after operator recovery"):
        request.prepare(preparer, later, later + request.timedelta(hours=2), "scripts-v1", receipt, later)
    with check("recovery cannot signal a new slot", "Only the independent preparer"):
        request.signal(recovery, recovery.read(request.REQUEST), later)
    with check("requester cannot start a new signal after expiry", "window"):
        request.request_and_wait(sender, later, sha, lambda: later + request.MAX_LIFETIME, unexpected)
    request.signal(sender, sender.read(request.REQUEST), later)
    with check("expired unclaimed request remains ineligible", "did not claim"):
        request.request_and_wait(sender, later, sha, lambda: later + request.MAX_LIFETIME, unexpected)
    obj = executor.read(request.REQUEST)
    request.acquire(executor, request.daily_owner(later, obj))
    request.set_state(executor, obj, "running")
    late = later + request.MAX_LIFETIME

    def finish_late(_):
        running = executor.read(request.REQUEST)
        with check("a claimed request stays observable after expiry"):
            if running["data"]["state"] != "running":
                raise AssertionError("Expected original running request")
        request.set_state(executor, running, "verified")
        verified_obj = executor.read(request.REQUEST)
        with check("operator finalizes interrupted verified outcome without another apply"):
            request.recover(
                recovery,
                later,
                "scripts-v1",
                receipt,
                snapshot(verified_obj, recovery.read(request.CHECKPOINT)),
                USERS[3],
                True,
            )

    with check("late observer receives the original verified deployment receipt"):
        if request.request_and_wait(sender, later, sha, lambda: late, finish_late) != receipt:
            raise AssertionError("Unexpected verified receipt")
    with check("completed rerun succeeds after expiry without another signal"):
        request.request_and_wait(sender, later, sha, lambda: late, unexpected)
    request.acquire(manual, "manual/3/1")
    with check(
        "recovery cannot release a foreign manual owner through the API", "only remove the matching"
    ):
        request.release(recovery, "manual/3/1")
    request.release(manual, "manual/3/1")
    return {
        "kubernetes": version,
        "passed": True,
        "cases": cases,
        "productionAccess": False,
        "appliedApplicationWorkload": False,
    }


def local_kind(args):
    raw = yaml.safe_load(args.kubeconfig.read_bytes())
    if (
        raw["current-context"] != CONTEXT
        or any([x["name"] for x in raw[k]] != [CONTEXT] for k in ("clusters", "contexts", "users"))
        or raw["contexts"][0]["context"].get("user") != CONTEXT
        or raw["contexts"][0]["context"].get("cluster") != CONTEXT
        or set(raw["users"][0]["user"]) != {"client-certificate-data", "client-key-data"}
    ):
        raise ValueError("Expected the dedicated local kind context")
    cluster = raw["clusters"][0]["cluster"]
    api = urlsplit(cluster["server"])
    if (
        set(cluster) != {"server", "certificate-authority-data"}
        or api.scheme != "https"
        or not api.port
        or not ipaddress.ip_address(api.hostname).is_loopback
        or api.username
        or api.password
        or api.path not in ("", "/")
        or api.query
        or api.fragment
        or b"BEGIN CERTIFICATE"
        not in base64.b64decode(cluster["certificate-authority-data"], validate=True)
    ):
        raise ValueError("Expected trusted loopback TLS")
    k = [args.kubectl, "--kubeconfig", str(args.kubeconfig), "--context", CONTEXT, "--request-timeout=20s"]
    if run_api(k, "get", "nodes", "-o", "name").strip() != "node/jaeger-request-rehearsal-control-plane":
        raise ValueError("Unexpected local node inventory")
    return k


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kubeconfig", type=Path, required=True)
    p.add_argument("--kubectl", default="kubectl")
    p.add_argument("--receipt", type=Path, required=True)
    args = p.parse_args()
    if args.receipt.exists():
        raise SystemExit("Supply a new receipt path")
    result = run_cases(local_kind(args))
    with args.receipt.open("x") as f:
        json.dump(result, f, indent=2)
        f.write("\n")
    print(f"Passed {len(result['cases'])} Kubernetes API checks")
