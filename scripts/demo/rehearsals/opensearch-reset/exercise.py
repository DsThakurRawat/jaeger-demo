#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Rehearse the OpenSearch trace reset on disposable Kubernetes.

Installs the pinned OpenSearch chart under the names the hosted demo uses, binds
a rehearsal user to the committed deployer roles, and runs the reset as that
user: the access check, a listing, the refusals and the deletion. A stand-in
Deployment takes the Jaeger restart. No Jaeger runs here, so the reset's last
step, the fresh trace through Jaeger, is expected to fail; production proves it.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import chart_archives
import deployer_access
import opensearch_reset as reset

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / "scripts/demo"
USER = "ocid1.user.oc1..aaaarehearsalmanual"
TRACES = ["jaeger-main-jaeger-dependencies-2026-01-01", "jaeger-main-jaeger-sampling-2026-01-01",
          "jaeger-main-jaeger-service-2026-01-01", "jaeger-main-jaeger-span-2026-01-01",
          "jaeger-main-jaeger-span-2026-01-02"]
OTHERS = [".kibana_1", "jaeger-archive-jaeger-span-2026-01-01", "jaeger-main-jaeger-span-2026-01-01-000001",
          "jaeger-main-jaeger-spans-2026-01-01", "unrelated-2026-01-01"]
TEMPLATE = "jaeger-main-jaeger-span"


def exercise(cfg, context):
    kubectl = ["kubectl", "--kubeconfig", str(cfg), "--context", context]
    helm = ["helm", "--kubeconfig", str(cfg), "--kube-context", context]

    def kube(args, body=None):
        return subprocess.run(kubectl + args, input=body, text=True, check=True, capture_output=True).stdout.strip()

    view = json.loads(kube(["config", "view", "--minify", "-o", "json"]))
    if urlsplit(view["clusters"][0]["cluster"]["server"]).hostname != "127.0.0.1":
        raise ValueError("This rehearsal requires a disposable loopback API server")
    namespaces = set(kube(["get", "ns", "-o", "jsonpath={.items[*].metadata.name}"]).split())
    if namespaces & {"opensearch", "jaeger", "otel-demo"}:
        raise ValueError("Use a new fixture without the demo namespaces")
    # The committed roles span these namespaces; only two of them hold anything here.
    for namespace in ("opensearch", "jaeger", "otel-demo"):
        kube(["create", "namespace", namespace])

    def engine(method, path, body=None):
        command = kubectl + ["-n", reset.NAMESPACE, "exec", reset.POD, "-c", reset.CONTAINER, "--",
                             "curl", "-fsS", "--max-time", "60", "-X", method, reset.API + path]
        if body is not None:
            command += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
        return json.loads(subprocess.run(command, text=True, check=True, capture_output=True).stdout)

    def indices():
        return sorted(row["index"] for row in engine("GET", "_cat/indices?format=json&h=index&expand_wildcards=all"))

    with tempfile.TemporaryDirectory() as temp:
        # The chart, values and object names of the hosted demo. Only size, storage class and memory differ.
        version = chart_archives.pin(ROOT, "OPENSEARCH_CHART_VERSION")
        archive = subprocess.run([sys.executable, str(SCRIPTS / "chart_archives.py"), "pull", "opensearch", version, temp],
                                 text=True, check=True, capture_output=True).stdout.strip()
        subprocess.run(helm + ["install", "opensearch", archive, "--namespace", reset.NAMESPACE,
                               "-f", str(ROOT / "examples/otel-demo/opensearch-values.yaml"),
                               "--set", "persistence.storageClass=standard", "--set", "persistence.size=1Gi",
                               "--set-string", "opensearchJavaOpts=-Xms512m -Xmx512m",
                               "--set", "resources.requests.cpu=100m", "--set", "resources.requests.memory=1Gi",
                               "--set", "resources.limits.memory=2Gi", "--set", "sysctlInit.enabled=true",
                               "--wait", "--timeout", "10m"], check=True, stdout=subprocess.DEVNULL)
        kube(["-n", reset.NAMESPACE, "rollout", "status", "statefulset/" + reset.STATEFULSET, "--timeout=5m"])
        for _ in range(60):
            try:
                if engine("GET", "_cluster/health").get("number_of_nodes") == 1:
                    break
            except subprocess.CalledProcessError:
                pass
            time.sleep(5)
        else:
            raise AssertionError("OpenSearch did not answer")

        # What Jaeger would have created, and what it and other components must keep.
        engine("PUT", "_index_template/" + TEMPLATE, {"index_patterns": [TEMPLATE + "-*"], "template": {
            "settings": {"number_of_shards": 1, "number_of_replicas": 0}}})
        for name in TRACES + OTHERS:
            engine("PUT", name, {"settings": {"number_of_shards": 1, "number_of_replicas": 0}})
        # The engine keeps indices of its own, and may add more while this runs.
        seeded = set(indices())
        assert set(TRACES + OTHERS) <= seeded, seeded
        kept = seeded - set(TRACES)

        # A stand-in for the Jaeger Deployment that the reset restarts.
        stub = {"apiVersion": "apps/v1", "kind": "Deployment",
                "metadata": {"name": reset.JAEGER_DEPLOYMENT, "namespace": reset.JAEGER_NAMESPACE},
                "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "jaeger-stand-in"}},
                         "template": {"metadata": {"labels": {"app": "jaeger-stand-in"}},
                                      "spec": {"terminationGracePeriodSeconds": 0, "containers": [{
                                          "name": "jaeger", "image": "docker.io/busybox:latest",
                                          "command": ["sleep", "3600"]}]}}}}
        workload = "deployment/" + reset.JAEGER_DEPLOYMENT
        kube(["apply", "-f", "-"], json.dumps(stub))
        kube(["-n", reset.JAEGER_NAMESPACE, "rollout", "status", workload, "--timeout=5m"])
        restarted = "{.spec.template.metadata.annotations.kubectl\\.kubernetes\\.io/restartedAt}"
        assert kube(["-n", reset.JAEGER_NAMESPACE, "get", workload, "-o", "jsonpath=" + restarted]) == ""

        # The committed roles, bound to a rehearsal user. Everything below runs as that user.
        kube(["apply", "-f", str(deployer_access.ROLES)])
        kube(["apply", "-f", "-"], json.dumps(deployer_access.bindings(USER)))
        config = yaml.safe_load(Path(cfg).read_text())
        config["current-context"] = context
        for user in config["users"]:
            user["user"]["as"] = USER
        manual = Path(temp) / "manual.yaml"
        manual.write_text(yaml.safe_dump(config))
        manual.chmod(0o600)

        def as_manual(script, argv, event="workflow_dispatch"):
            env = dict(os.environ, KUBECONFIG=str(manual), GITHUB_EVENT_NAME=event, GITHUB_REF="refs/heads/main")
            env.pop("GITHUB_STEP_SUMMARY", None)
            return subprocess.run([sys.executable, str(SCRIPTS / script)] + argv, env=env, text=True, capture_output=True)

        who = subprocess.run(["kubectl", "auth", "whoami", "-o", "jsonpath={.status.userInfo.username}"],
                             env=dict(os.environ, KUBECONFIG=str(manual)), text=True, check=True, capture_output=True)
        assert who.stdout.strip() == USER, who.stdout
        access = as_manual("deployer_access.py", ["check", "--self", "--identity", "manual"])
        assert access.returncode == 0 and "manual: 0 difference(s)" in access.stdout, access.stdout + access.stderr

        listed = as_manual("opensearch_reset.py", ["list"])
        assert listed.returncode == 0, listed.stderr
        assert all(name in listed.stdout.splitlines() for name in TRACES), listed.stdout
        assert not any(name in listed.stdout.splitlines() for name in OTHERS), listed.stdout

        accepted = ["reset", "--acknowledge", reset.ACKNOWLEDGMENT]
        refusals = [(["reset"], "workflow_dispatch"), (["reset", "--acknowledge", "yes"], "workflow_dispatch"),
                    (["reset", "--acknowledge", reset.ACKNOWLEDGMENT.lower()], "workflow_dispatch"),
                    (accepted, "schedule"), (accepted, "push")]
        for argv, event in refusals:
            refused = as_manual("opensearch_reset.py", argv, event)
            assert refused.returncode == 2 and "Refused, nothing was deleted" in refused.stderr, (argv, event, refused.stderr)
        assert seeded <= set(indices()), "A listing or a refused request removed an index"
        assert kube(["-n", reset.JAEGER_NAMESPACE, "get", workload, "-o", "jsonpath=" + restarted]) == ""

        # The reset itself. It deletes and restarts, then fails only where it needs a real Jaeger.
        result = as_manual("opensearch_reset.py", accepted)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Fresh deployment trace could not be written and retrieved" in result.stderr, result.stderr
        for name in TRACES:
            assert "Deleted " + name in result.stdout.splitlines(), result.stdout
        remaining = set(indices())
        assert not remaining & set(TRACES) and kept <= remaining, remaining
        assert [row["name"] for row in engine("GET", "_cat/templates/jaeger*?format=json&h=name")] == [TEMPLATE]
        assert kube(["-n", reset.JAEGER_NAMESPACE, "get", workload, "-o", "jsonpath=" + restarted]) != ""
        kube(["-n", reset.JAEGER_NAMESPACE, "rollout", "status", workload, "--timeout=5m"])

        # The surviving template shapes the next index that ingestion creates.
        renewed = TEMPLATE + "-2026-01-03"
        engine("POST", renewed + "/_doc?refresh=true", {"traceID": "1"})
        settings = engine("GET", renewed + "/_settings")[renewed]["settings"]["index"]
        assert settings["number_of_replicas"] == "0", settings

        # A second run finds only what was written since, and the role still cannot touch storage.
        again = as_manual("opensearch_reset.py", ["list"])
        assert again.returncode == 0 and renewed in again.stdout.splitlines(), again.stdout + again.stderr
        for verb, resource in (("delete", "persistentvolumeclaims"), ("delete", "pods"), ("create", "pods")):
            answer = subprocess.run(["kubectl", "-n", reset.NAMESPACE, "auth", "can-i", verb, resource],
                                    env=dict(os.environ, KUBECONFIG=str(manual)), text=True, capture_output=True)
            assert answer.stdout.strip() == "no", (verb, resource, answer.stdout)

    print(json.dumps({"chart": "opensearch-" + version, "accessCheckDifferences": 0, "refusedRequests": len(refusals),
                      "deletedTraceIndices": len(TRACES), "keptIndices": len(kept),
                      "templateKept": True, "jaegerStandInRestarted": True, "freshTraceCheck": "not rehearsed"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True, choices=("kind-jaeger-demo-reset-fixture",))
    args = parser.parse_args()
    if not __debug__:
        parser.error("Rehearsal assertions must be enabled")
    exercise(args.kubeconfig.resolve(), args.context)
