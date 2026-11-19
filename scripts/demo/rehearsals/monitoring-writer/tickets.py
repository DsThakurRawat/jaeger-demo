# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Exercise two operator-prepared requests on the disposable local fixture."""

import hashlib
import json
from pathlib import Path
import subprocess

import yaml

NAMESPACE = "demo-executor-fixture"
REQUESTER = "scheduled-requester"
CONFIG = "ce3cf3a8fd331ec36a8e0b1ba41a59d24b42f447"
DENIAL = "The requester may only signal an existing reviewed operation once."
OPERATIONS = {"upgrade": (1, 2, ""), "rollback": (2, 3, "1")}


def checked(args, body=None, expect_error=None, env=None):
    r = subprocess.run(
        args,
        input=None if body is None else json.dumps(body),
        text=True,
        env=env,
        capture_output=True,
        timeout=40,
    )
    if expect_error is not None:
        if r.returncode == 0 or expect_error not in r.stderr:
            raise RuntimeError("Expected fixture denial: " + str(args[-5:-2]))
        return None
    if r.returncode:
        # Never include request bodies, tokens, or the contents of release records.
        raise RuntimeError("Fixture command failed: " + r.stderr.splitlines()[0][:200])
    return r.stdout


def check_ticket(ticket, prepared, uid):
    if (
        ticket["metadata"]["uid"] != uid
        or ticket["data"] != dict(prepared, requested="true")
        or ticket.get("binaryData")
    ):
        raise RuntimeError("Request is stale, changed, unrequested, or already claimed")


class TriggerFixture:
    def __init__(self, admin, kubeconfig, home, pkg_sha, env):
        self.admin = admin
        self.env = env
        self.pkg_sha = pkg_sha
        self.evidence = {}
        # The operator is the only writer of request definitions and the policy.
        manifests = yaml.safe_load_all(
            Path(__file__).with_name("trigger-policy.yaml").read_bytes()
        )
        for doc in manifests:
            checked(admin + ["create", "-f", "-"], doc, env=env)
        token = checked(
            admin
            + ["-n", NAMESPACE, "create", "token", REQUESTER, "--duration", "20m"],
            env=env,
        ).strip()
        raw = yaml.safe_load(kubeconfig.read_bytes())
        config = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": raw["clusters"],
            "users": [{"name": REQUESTER, "user": {"token": token}}],
            "contexts": [
                {
                    "name": "requester",
                    "context": {
                        "cluster": raw["clusters"][0]["name"],
                        "user": REQUESTER,
                        "namespace": NAMESPACE,
                    },
                }
            ],
            "current-context": "requester",
        }
        path = Path(home) / "requester-kubeconfig"
        path.write_text(json.dumps(config), encoding="utf-8")
        path.chmod(0o600)
        self.client = [
            admin[0],
            "--kubeconfig",
            str(path),
            "--context",
            "requester",
            "--cache-dir",
            home + "/requester-cache",
            "--request-timeout=30s",
        ]
        identity = json.loads(
            checked(self.client + ["auth", "whoami", "-o", "json"], env=env)
        )
        if (
            identity["status"]["userInfo"]["username"]
            != "system:serviceaccount:" + NAMESPACE + ":" + REQUESTER
        ):
            raise RuntimeError("Unexpected requester identity")
        denied = {
            "release Secret read": [
                "-n",
                "default",
                "get",
                "secret",
                "sh.helm.release.v1.prometheus.v1",
            ],
            "release Secret list": ["-n", "default", "get", "secrets"],
            "unrelated Secret read": [
                "-n",
                "default",
                "get",
                "secret",
                "jaeger-demo-fixture",
            ],
            "Pod reads": ["-n", "default", "get", "pods"],
            "new request creation": [
                "-n",
                NAMESPACE,
                "create",
                "configmap",
                "unreviewed",
                "--dry-run=server",
            ],
            "chart Job deletion": [
                "-n",
                "default",
                "delete",
                "job",
                "prometheus-crds-upgrade",
                "--dry-run=server",
            ],
            "restricting policy changes": [
                "patch",
                "validatingadmissionpolicy",
                "fixture-reviewed-monitoring-request",
                "--type=merge",
                "-p",
                '{"metadata":{"labels":{"fixture":"test"}}}',
                "--dry-run=server",
            ],
            "monitoring webhook changes": [
                "patch",
                "validatingwebhookconfiguration",
                "prometheus-kube-prometheus-admission",
                "--type=merge",
                "-p",
                '{"metadata":{"labels":{"fixture":"test"}}}',
                "--dry-run=server",
            ],
            "operator changes": [
                "-n",
                "default",
                "patch",
                "deployment",
                "prometheus-kube-prometheus-operator",
                "--type=merge",
                "-p",
                '{"metadata":{"labels":{"fixture":"test"}}}',
                "--dry-run=server",
            ],
            "Role changes": [
                "-n",
                NAMESPACE,
                "patch",
                "role",
                "signal-reviewed-monitoring-request",
                "--type=merge",
                "-p",
                '{"metadata":{"labels":{"fixture":"test"}}}',
                "--dry-run=server",
            ],
        }
        for cmd in denied.values():
            checked(self.client + cmd, expect_error="forbidden", env=env)
        self.evidence["directAccessDenials"] = list(denied)

    def release(self, revision):
        name = "sh.helm.release.v1.prometheus.v" + str(revision)
        obj = json.loads(
            checked(
                self.admin + ["-n", "default", "get", "secret", name, "-o", "json"],
                env=self.env,
            )
        )
        if (
            obj["type"] != "helm.sh/release.v1"
            or obj["metadata"]["labels"]["name"] != "prometheus"
            or obj["metadata"]["labels"]["version"] != str(revision)
        ):
            raise RuntimeError("Unexpected stored release")
        return hashlib.sha256(obj["data"]["release"].encode()).hexdigest()

    def claim(self, action, revision, helm_history):
        previous, expected, rollback = OPERATIONS[action]
        if expected != revision:
            raise RuntimeError("Invalid operator action order")
        if [(int(x["revision"]), x["status"]) for x in helm_history][-1] != (
            previous,
            "deployed",
        ):
            raise RuntimeError("Unexpected live revision before request")
        name = "monitoring-" + action
        prepared = {
            "action": action,
            "release": "default/prometheus",
            "configSHA": CONFIG,
            "packageSHA256": self.pkg_sha,
            "priorRevision": str(previous),
            "priorReleaseSHA256": self.release(previous),
            "rollbackRevision": rollback,
            "rollbackReleaseSHA256": self.release(int(rollback)) if rollback else "",
            "requested": "false",
            "state": "prepared",
        }
        obj = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": NAMESPACE},
            "data": prepared,
        }
        ticket = json.loads(
            checked(self.admin + ["create", "-f", "-", "-o", "json"], obj, env=self.env)
        )
        uid = ticket["metadata"]["uid"]
        patch = self.client + [
            "-n",
            NAMESPACE,
            "patch",
            "configmap",
            name,
            "--type=merge",
            "-p",
        ]
        wrong = {"data": {"requested": "true", "packageSHA256": "fixture-alternate"}}
        checked(
            patch + [json.dumps(wrong), "--dry-run=server"],
            expect_error=DENIAL,
            env=self.env,
        )
        extra = {"data": {"requested": "true", "extra": "test"}}
        checked(
            patch + [json.dumps(extra), "--dry-run=server"],
            expect_error=DENIAL,
            env=self.env,
        )
        checked(patch + [json.dumps({"data": {"requested": "true"}})], env=self.env)
        # The executor reads the request back independently before claiming it.
        read = self.admin + ["-n", NAMESPACE, "get", "configmap", name, "-o", "json"]
        accepted = json.loads(checked(read, env=self.env))
        check_ticket(accepted, prepared, uid)
        if self.release(previous) != prepared["priorReleaseSHA256"] or (
            rollback
            and self.release(int(rollback)) != prepared["rollbackReleaseSHA256"]
        ):
            raise RuntimeError("Release inputs changed after the request was signalled")
        claim = [
            {"op": "test", "path": "/metadata/uid", "value": uid},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": accepted["metadata"]["resourceVersion"],
            },
            {"op": "test", "path": "/data/state", "value": "prepared"},
            {"op": "replace", "path": "/data/state", "value": "claimed"},
        ]
        claim_cmd = self.admin + [
            "-n",
            NAMESPACE,
            "patch",
            "configmap",
            name,
            "--type=json",
            "-p",
            json.dumps(claim),
        ]
        checked(claim_cmd, env=self.env)
        # Kubernetes must reject a duplicate executor claim of the same version.
        duplicate = subprocess.run(
            claim_cmd, env=self.env, capture_output=True, timeout=35
        )
        if duplicate.returncode == 0:
            raise RuntimeError("Duplicate executor claim succeeded")
        current = json.loads(checked(read, env=self.env))
        if (
            current["metadata"]["uid"] != uid
            or current["data"] != dict(prepared, requested="true", state="claimed")
            or current["metadata"]["resourceVersion"]
            == accepted["metadata"]["resourceVersion"]
        ):
            raise RuntimeError("Executor claim was not durably recorded")
        checked(
            patch + [json.dumps({"data": {"requested": "true"}}), "--dry-run=server"],
            expect_error=DENIAL,
            env=self.env,
        )
        self.evidence[action] = {
            "sourceRevision": previous,
            "expectedRevision": revision,
            "rollbackTarget": rollback or None,
            "storedInputDigestsMatched": True,
            "changedInputsDenied": True,
            "extraInputDenied": True,
            "duplicateSignalDenied": True,
            "duplicateClaimDenied": True,
        }
