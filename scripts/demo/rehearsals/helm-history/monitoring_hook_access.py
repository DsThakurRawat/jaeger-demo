# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Exercise the real hook inventory behind the candidate executor's admission and RBAC."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "ops/executor"))
import monitoring_hooks as hooks
import permissions as r


def exercise(archive, kubectl, cfg, kube):
    account = ["--as", r.USER, "--as-group", "system:authenticated",
               "--as-group", "system:serviceaccounts", "--as-group", "system:serviceaccounts:" + r.OPS]
    kube(["create", "namespace", r.OPS])
    checkpoint = r.resource("v1", "ConfigMap", "jaeger-demo-deployment-revisions", "default")
    checkpoint["metadata"]["annotations"] = {r.PROTOCOL: "v1", r.HOLDER: "daily/2026-11-19T13:00:00Z/fixture"}
    kube(["create", "-f", "-"], body=json.dumps(checkpoint))
    cm = r.resource("v1", "ConfigMap", hooks.REQUEST, r.OPS, data={"state": "running", "engine": "scripts-v1", "slot": "2026-11-19T13:00:00Z"})
    kube(["create", "-f", "-"], body=json.dumps(cm))
    for item in r.bindings("fixture-read-request", r.OPS, [r.rule("", "configmaps", ["get"], [hooks.REQUEST])], (r.EXECUTOR,)):
        kube(["create", "-f", "-"], body=json.dumps(item))
    definitions = hooks.pinned_hooks(archive)
    defaulted = hooks.dry_admitted(definitions, [kubectl, "--kubeconfig", str(cfg), "--context", "fixture"])
    guards, grants = hooks.controls(defaulted)
    for item in guards:
        kube(["create", "-f", "-"], body=json.dumps(item))
    # The implicit CREATE RBAC on hooks must be paired with a working name guard.
    # The same independent API fixture supplies the scoped rights before nonpersisting probes.
    for item in grants:
        kube(["create", "-f", "-"], body=json.dumps(item))

    def trial(obj, *, dry=True):
        args = [*account, "create", "--field-manager=helm", "-f", "-"]
        if dry:
            args.append("--dry-run=server")
        return kube(args, body=json.dumps(obj), check=False)

    negatives = []
    for target in ("clusterroles.rbac.authorization.k8s.io/prometheus-crds-upgrade",
                   "roles.rbac.authorization.k8s.io/prometheus-kube-prometheus-admission"):
        named = kube([*account, "auth", "can-i", "escalate", target, "-n", "default"], check=False)
        if named.stdout.strip() != "no":
            raise ValueError("Escalation on an existing named Role was allowed")
    negatives.append("escalation on existing named Roles")

    probes = {"Job": lambda o: o["spec"]["template"]["spec"]["containers"][0].update({"image": "registry.invalid/fixture:wrong"}),
              "ClusterRoleBinding": lambda o: o["subjects"][0].update({"name": "different-sa"}),
              "ClusterRole": lambda o: o["rules"][0].update({"verbs": ["*"]}),
              "ConfigMap": lambda o: o["binaryData"].update({"crds.bz2": "YQ=="}),
              "ServiceAccount": lambda o: o.update({"automountServiceAccountToken": False}),
              "Role": lambda o: o["rules"][0].update({"verbs": ["*"]}),
              "RoleBinding": lambda o: o["subjects"][0].update({"namespace": "unexpected"})}
    unrelated = deepcopy(definitions[0])
    unrelated["metadata"]["name"] = "unrelated-hook"
    for _ in range(80):
        denied = trial(unrelated)
        if denied.returncode and "Only the twelve reviewed monitoring hooks" in denied.stderr:
            break
        time.sleep(.25)
    else:
        raise ValueError("Expected the independent hook name policy: " + denied.stderr[:500])
    negatives.append("unrelated name")
    for kind, change in probes.items():
        obj = deepcopy(next(obj for obj in definitions if obj["kind"] == kind))
        change(obj)
        rejected = trial(obj)
        if not rejected.returncode or "Hook differs" not in rejected.stderr:
            raise ValueError(f"Expected content policy denial for altered {kind}: {rejected.stderr[:400]}")
        negatives.append("altered " + kind)
    changed_command = deepcopy(next(o for o in definitions if o["kind"] == "Job"))
    changed_command["spec"]["template"]["spec"]["containers"][0]["command"] = ["/bin/false"]
    blocked_command = trial(changed_command)
    if not blocked_command.returncode or "Hook differs" not in blocked_command.stderr:
        raise ValueError("Altered hook command was not denied")
    negatives.append("altered hook command")
    for obj in definitions:
        got = trial(obj, dry=False)
        if got.returncode:
            raise ValueError("Valid " + obj["kind"] + " denied: " + got.stderr[:700])
    # A pinned hook's own Kubernetes account must also preserve the approved endpoint and credentials.
    webhook = r.resource(r.ADMISSION, "ValidatingWebhookConfiguration", "prometheus-kube-prometheus-admission",
            webhooks=[{"name": "monitor.fixture.example", "clientConfig": {"url": "https://fixture.invalid/hook"},
                       "rules": [], "failurePolicy": "Ignore", "admissionReviewVersions": ["v1"], "sideEffects": "None"}])
    kube(["create", "-f", "-"], body=json.dumps(webhook))
    account_args = ["--as", "system:serviceaccount:default:prometheus-kube-prometheus-admission", "--as-group", "system:authenticated"]
    def hook_patch(patch, *, allowed=False):
        response = kube([*account_args, "patch", "validatingwebhookconfiguration", webhook["metadata"]["name"],
                         "--type=json", "-p", json.dumps(patch), "--dry-run=server"], check=False)
        if allowed and response.returncode or not allowed and (not response.returncode or "must preserve" not in response.stderr):
            raise ValueError("Unexpected hook webhook decision: " + response.stderr[:360])
    hook_patch([{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Ignore"}], allowed=True)
    changed_policy = [{"op":"replace","path":"/webhooks/0/failurePolicy","value":"Fail"}]
    hook_patch(changed_policy)
    import base64
    config = json.loads(Path(cfg).read_bytes())
    trust = config["clusters"][0]["cluster"]
    ca = trust.get("certificate-authority-data") or base64.b64encode(Path(trust["certificate-authority"]).read_bytes()).decode()
    hook_patch([{"op":"add","path":"/webhooks/0/clientConfig/caBundle","value":ca}])
    bad = kube([*account_args, "-n", "default", "create", "secret", "generic", "unreviewed", "--from-literal=x=fixture",
                "--dry-run=server"], check=False)
    if not bad.returncode or "must preserve" not in bad.stderr:
        raise ValueError("Daily certificate hook Secret create was not denied")
    crd_name = "prometheusrules.monitoring.coreos.com"
    crd = r.resource("apiextensions.k8s.io/v1", "CustomResourceDefinition", crd_name, spec={
        "group":"monitoring.coreos.com","scope":"Namespaced","names":{"plural":"prometheusrules","kind":"PrometheusRule"},
        "versions":[{"name":"v1","served":True,"storage":True,"schema":{"openAPIV3Schema":{"type":"object"}}}]})
    kube(["create", "-f", "-"], body=json.dumps(crd))
    wrong_crd = kube(["--as", "system:serviceaccount:default:prometheus-crds-upgrade", "--as-group", "system:authenticated",
                      "patch", "crd", crd_name, "--dry-run=server", "--type=merge", "-p",
                      '{"spec":{"names":{"shortNames":["unexpected"]}}}'], check=False)
    if not wrong_crd.returncode or "must preserve" not in wrong_crd.stderr:
        raise ValueError("Changed CRD spec was not denied: " + wrong_crd.stderr[:300])
    negatives.extend(["hook CA replacement","webhook field change", "hook certificate creation", "hook CRD schema/definition change"])
    kube(["-n", "default", "annotate", "configmap", checkpoint["metadata"]["name"], "jaegertracing.io/deployment-lock-holder-"])
    hook_patch(changed_policy, allowed=True)
    locked = trial(definitions[0])
    if not locked.returncode or "daily checkpoint holder" not in locked.stderr:
        raise ValueError("Expected missing daily holder to deny hook write: " + locked.stderr[:320])
    negatives.append("missing daily holder")
    kube(["-n", "default", "annotate", "configmap", checkpoint["metadata"]["name"],
          r.HOLDER + "=daily/2026-11-19T13:00:00Z/fixture"])
    # envtest runs no controllers, so the Jobs remain inert in this API.
    for obj in reversed(definitions):
        args = [*account, "delete", obj["kind"], obj["metadata"]["name"], "--wait=false"]
        if not obj["kind"].startswith("Cluster"):
            args += ["-n", "default"]
        kube(args)
    kube(["-n", r.OPS, "patch", "configmap", hooks.REQUEST, "--type=merge", "-p", '{"data":{"state":"ready"}}'])
    blocked = trial(definitions[0])
    if not blocked.returncode or "protected script request must be running" not in blocked.stderr:
        raise ValueError("Hook accepted outside the protected operation")
    negatives.append("no running request")
    return {"created": len(definitions), "deleted": len(definitions), "denied": negatives, "controlPlaneOnly": True}
