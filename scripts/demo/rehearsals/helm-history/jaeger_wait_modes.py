# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Compare real Helm readiness strategies with the named scheduled read set."""

import json

import scheduled_adapter as adapter


def workload_status(kube, namespace, name, *, ready):
    deployment = json.loads(kube(["get", "deployment", name, "-n", namespace, "-o", "json"]).stdout)
    metadata = deployment["metadata"]
    spec = deployment["spec"]
    count = spec["replicas"]
    rs_name = name + "-fixture"
    rs = {
        "apiVersion": "apps/v1", "kind": "ReplicaSet",
        "metadata": {
            "name": rs_name, "namespace": namespace,
            "labels": spec["template"]["metadata"]["labels"],
            "ownerReferences": [{"apiVersion": "apps/v1", "kind": "Deployment", "name": name,
                                 "uid": metadata["uid"], "controller": True, "blockOwnerDeletion": True}],
        },
        "spec": {"selector": spec["selector"], "template": spec["template"], "replicas": count},
    }
    rs = json.loads(kube(["apply", "-f", "-", "-o", "json"], body=json.dumps(rs)).stdout)
    for kind, obj in (("replicaset", rs), ("deployment", deployment)):
        status = {"observedGeneration": obj["metadata"]["generation"], "replicas": count,
                  "readyReplicas": count if ready else 0, "availableReplicas": count if ready else 0}
        if kind == "deployment":
            status.update(updatedReplicas=count, conditions=[
                {"type": "Available", "status": "True" if ready else "False", "reason": "FixtureControlled"}])
        else:
            status["fullyLabeledReplicas"] = count
        kube(["patch", kind, obj["metadata"]["name"], "-n", namespace, "--subresource=status",
              "--type=merge", "--patch-file=/dev/stdin"], body=json.dumps({"status": status}))


def check(kube, namespace, user, role, named_rules, args, env, audit, values):
    rules = [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["create", "update", "delete", "get", "list"]},
             {"apiGroups": ["apps"], "resources": ["deployments", "replicasets"], "verbs": ["get", "list", "watch"]},
             {"apiGroups": [""], "resources": ["pods"], "verbs": ["get", "list", "watch"]}]
    for rule in named_rules:
        rules.append(dict(rule, verbs=["get", "patch"]))
    role(rules)

    def attempt(mode, ready):
        for name in ("jaeger", "jaeger-hotrod"):
            workload_status(kube, namespace, name, ready=ready)
        offset = audit.stat().st_size
        cmd = [arg for arg in args if not str(arg).startswith("--wait=")] + [
            "--post-renderer", "verify-frozen-manifests", "--wait=" + mode, "--timeout", "5s"]
        try:
            adapter.command(cmd, body=adapter.encode(values), env=env, timeout=15)
            passed = True
        except adapter.AdapterError:
            passed = False
        denied = set()
        with audit.open("rb") as stream:
            stream.seek(offset)
            for line in stream:
                event = json.loads(line)
                if (event.get("impersonatedUser", {}).get("username") == user and
                    event.get("responseStatus", {}).get("code") == 403):
                    denied.add(event["verb"] + " " + event["objectRef"]["resource"])
        return {"succeeded": passed, "denied": sorted(denied)}

    watcher = attempt("watcher", True)
    healthy = attempt("legacy", True)
    unready = attempt("legacy", False)
    print("ready-wait audit", namespace, watcher, healthy, unready, flush=True)
    assert not watcher["succeeded"] and watcher["denied"]
    assert healthy == {"succeeded": True, "denied": []}
    assert unready == {"succeeded": False, "denied": []}
    return {"readyWatcher": watcher, "readyLegacy": healthy, "unreadyLegacy": unready,
            "fixture": "actual chart and stored objects; API fixture supplies Ready or unready Deployment/ReplicaSet status"}
