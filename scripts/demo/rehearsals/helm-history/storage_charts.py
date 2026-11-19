# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Exercise in-place Helm updates to the real storage charts on the local test API."""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sys
import time
from unittest.mock import patch

from storage_admission import DENIED, MARKER, enforce


ROOT = Path(__file__).resolve().parents[4]
NS = "opensearch"
STS = "opensearch-cluster-single"
POD = STS + "-0"
CLAIM = STS + "-" + POD
PV = "fixture-bound-storage"
RBAC = "rbac.authorization.k8s.io"
CHECK = MARKER
CHARTS = {"opensearch": "opensearch-values.yaml", "opensearch-dashboards": "opensearch-dashboard-values.yaml"}
NAMES = {
    ("", "configmaps"): [STS + "-config", "opensearch-dashboards-config"],
    ("", "services"): [STS, STS + "-headless", "opensearch-dashboards"],
    ("", "serviceaccounts"): ["opensearch-dashboards-dashboards"],
    ("policy", "poddisruptionbudgets"): [STS + "-pdb"],
    ("apps", "statefulsets"): [STS],
    ("apps", "deployments"): ["opensearch-dashboards"],
    (RBAC, "rolebindings"): ["opensearch-dashboards-dashboards-rolebinding"],
}


def rehearse(archives, root, kube, release_cmd, writer):
    started = datetime.now(timezone.utc)
    chart_digests = json.loads((ROOT / "docs/all-five-chart-access.json").read_text())["chartArchiveSHA256"]
    values = ROOT / "examples/otel-demo"
    used = {}
    for name in CHARTS:
        path = archives / (name + "-3.7.0.tgz")
        used[name] = sha256(path.read_bytes()).hexdigest()
        if used[name] != chart_digests[path.name]:
            raise ValueError(f"Unexpected public chart archive digest for {name}")

    def admin(doc):
        return kube(["apply", "-f", "-"], body=json.dumps(doc))

    def chart(name, operation, extra=(), restricted=True):
        return release_cmd([operation, name, archives / (name + "-3.7.0.tgz"),
                            "-f", values / CHARTS[name], "--wait=hookOnly", *extra], NS, writer=restricted)

    def api(resource, name, restricted=True):
        return json.loads(kube(["-n", NS, "get", resource, name, "-o", "json"], writer=restricted).stdout)

    def deny(label, args, reason="forbidden", body=None):
        result = kube(["-n", NS, *args], writer=True, check=False, body=body)
        if result.returncode == 0 or reason.lower() not in result.stderr.lower():
            raise ValueError(f"Expected {label} denial: {result.stderr}")
        denials.append(label)

    # Retire the synthetic history bindings. Only the storage namespace will
    # bind this fixture user while the two real charts are exercised.
    for namespace in ["default", "jaeger", NS, "otel-demo"]:
        kube(["-n", namespace, "delete", "rolebinding", "fixture-history"])
    for name in CHARTS:
        release_cmd(["uninstall", name], NS, writer=False)
        chart(name, "install", restricted=False)

    # The API-only harness has no StatefulSet, storage provisioner or kubelet.
    # Seed the mounted-Pod and prebound PVC/PV inputs that a real cluster supplies.
    admin({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "default", "namespace": NS}})
    admin({"apiVersion": "v1", "kind": "PersistentVolume", "metadata": {"name": PV}, "spec": {
        "capacity": {"storage": "50Gi"}, "accessModes": ["ReadWriteOnce"],
        "persistentVolumeReclaimPolicy": "Retain", "storageClassName": "oci-bv",
        "hostPath": {"path": "/unmounted-fixture"}, "claimRef": {"name": CLAIM, "namespace": NS}}})
    admin({"apiVersion": "v1", "kind": "PersistentVolumeClaim", "metadata": {"name": CLAIM, "namespace": NS},
           "spec": {"storageClassName": "oci-bv", "volumeName": PV, "accessModes": ["ReadWriteOnce"],
                    "resources": {"requests": {"storage": "50Gi"}}}})
    kube(["-n", NS, "patch", "pvc", CLAIM, "--subresource=status", "--type=merge", "-p",
          json.dumps({"status": {"phase": "Bound", "capacity": {"storage": "50Gi"},
                                 "accessModes": ["ReadWriteOnce"]}})])
    admin({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": POD, "namespace": NS}, "spec": {
        "automountServiceAccountToken": False,
        "containers": [{"name": "opensearch", "image": "example.invalid/fixture:1",
                        "volumeMounts": [{"name": STS, "mountPath": "/usr/share/opensearch/data"}]}],
        "volumes": [{"name": STS, "persistentVolumeClaim": {"claimName": CLAIM}}]}})

    # The real chart supplies a RoleBinding referencing an external Role.
    # A zero-rule fixture Role lets the changed-subject denial reach validation.
    admin({"apiVersion": RBAC + "/v1", "kind": "Role",
           "metadata": {"name": "opensearch-dashboards-dashboards", "namespace": NS}, "rules": []})

    rules = [{"apiGroups": [group], "resources": [kind], "resourceNames": names,
              "verbs": ["get", "patch"]}
             for (group, kind), names in NAMES.items()]
    rules += [
        {"apiGroups": [""], "resources": ["secrets"], "verbs": ["get", "list", "create", "update"]},
        {"apiGroups": [""], "resources": ["pods"], "resourceNames": [POD], "verbs": ["get"]},
        {"apiGroups": [""], "resources": ["persistentvolumeclaims"], "resourceNames": [CLAIM], "verbs": ["get"]},
    ]
    admin({"apiVersion": RBAC + "/v1", "kind": "Role", "metadata": {"name": "storage-writer", "namespace": NS},
           "rules": rules})
    admin({"apiVersion": RBAC + "/v1", "kind": "RoleBinding", "metadata": {"name": "storage-writer", "namespace": NS},
           "roleRef": {"apiGroup": RBAC, "kind": "Role", "name": "storage-writer"},
           "subjects": [{"apiGroup": RBAC, "kind": "User", "name": writer}]})
    # Run the same production Pod -> mounted PVC -> bound PV inspection. Route
    # subprocesses through the local Kube helper with the restricted identity.
    sys.path.insert(0, str(ROOT / "scripts/demo"))
    import deploy

    def binding():
        with patch.object(deploy, "run", lambda cmd, **kwargs: kube(cmd[1:], writer=True)):
            return deploy.opensearch_data_binding()

    def inventory():
        objects = {}
        for (_, kind), names in NAMES.items():
            for name in names:
                obj = api(kind, name)
                objects[(kind, name)] = (obj["metadata"]["uid"], obj.get("spec"), obj.get("data"),
                                         obj.get("roleRef"), obj.get("subjects"))
        return objects

    def service_marker(name):
        obj = api("service", STS if name == NS else name)
        return obj["metadata"].get("annotations", {}).get(CHECK)

    baseline = [api(kind, name) for (_, kind), names in NAMES.items() for name in names]
    with enforce(root, baseline, admin, writer) as decisions:
        original = inventory()
        bound = binding()
        assert bound[0] == CLAIM and bound[2] == PV
        assert original[("statefulsets", STS)][1]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"] == "10Gi"
        assert api("pvc", CLAIM)["status"]["capacity"]["storage"] == "50Gi"
        denials = []
        changed_selector = ["patch", "service", STS, "--dry-run=server", "--type=merge",
                            "-p", '{"spec":{"selector":{"fixture-routes":"other"}}}']
        for _ in range(60):
            result = kube(["-n", NS, *changed_selector], writer=True, check=False)
            if result.returncode and DENIED in result.stderr:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("Storage Service admission failed to enforce: " + result.stderr)

        deny("foreign Service selector", changed_selector, DENIED)
        deny("foreign Service annotation", ["annotate", "service", STS, "unreviewed=value", "--dry-run=server"],
             DENIED)
        deny("PVC change", ["annotate", "pvc", CLAIM, "fixture=value", "--dry-run=server"])
        deny("PVC deletion", ["delete", "pvc", CLAIM, "--dry-run=server"])
        deny("namespace access", ["get", "secrets", "-n", "jaeger"])
        deny("workload metadata outside approved pair", ["annotate", "sts", STS, "fixture=value", "--dry-run=server"], DENIED)
        deny("unreviewed Dashboards image", [
            "set", "image", "deploy/opensearch-dashboards", "dashboards=example.invalid/unreviewed:fixture",
            "--dry-run=server"], DENIED)
        deny("changed RoleBinding subject", [
            "patch", "rolebinding", "opensearch-dashboards-dashboards-rolebinding", "--dry-run=server",
            "--type=json", "-p", '[{"op":"replace","path":"/subjects/0/name","value":"fixture-other"}]'], DENIED)
        deny("PVC expansion", ["patch", "pvc", CLAIM, "--dry-run=server", "--type=merge",
                               "-p", '{"spec":{"resources":{"requests":{"storage":"60Gi"}}}}'])
        deny("arbitrary Pod creation", ["create", "--dry-run=server", "-f", "-"],
             body=json.dumps({"apiVersion": "v1", "kind": "Pod", "metadata": {"name": "fixture-other"},
                              "spec": {"containers": [{"name": "example", "image": "example.invalid/test:0"}]}}))

        outcomes = {}
        for name in CHARTS:
            chart(name, "upgrade", ["--set-string", "service.annotations." + CHECK + "=verified",
                                    "--set-string", "podAnnotations." + CHECK + "=verified"])
            expected = deepcopy(original)
            workload = ("statefulsets", STS) if name == NS else ("deployments", name)
            expected[workload][1]["template"]["metadata"].setdefault("annotations", {})[MARKER] = "verified"
            assert service_marker(name) == "verified" and binding() == bound and inventory() == expected
            # Revert the approved metadata only at the same chart and engine
            # version. This is not a storage engine downgrade rehearsal.
            release_cmd(["rollback", name, "1", "--wait=hookOnly"], NS, writer=True)
            assert service_marker(name) is None and binding() == bound and inventory() == original
            ledger = json.loads(release_cmd(["history", name, "-o", "json"], NS).stdout)
            assert [(r["revision"], r["status"]) for r in ledger] == [(1, "superseded"), (2, "superseded"), (3, "deployed")]
            outcomes[name] = {"chart": "3.7.0", "upgradeRevision": 2, "revertedAnnotationsRevision": 3}

    requests = Counter()
    for line in (root / "audit.log").read_text().splitlines():
        event = json.loads(line)
        # The audit identity is the admin for an impersonated request.
        if event.get("impersonatedUser", {}).get("username") != writer:
            continue
        if datetime.fromisoformat(event["requestReceivedTimestamp"].replace("Z", "+00:00")) < started:
            continue
        ref = event.get("objectRef", {})
        if ref.get("resource"):
            requests[(event["verb"], ref["resource"], ref.get("subresource", ""), event["responseStatus"]["code"])] += 1
    for _, kind in NAMES:
        assert requests[("patch", kind, "", 200)] >= 2, f"Missing Helm API evidence for {kind}"
    assert decisions["allowed"] >= 20 and decisions["denied"] >= 5
    return {"scope": "API-only Service and pod-template annotation updates; same-version rollback; seeded storage binding",
            "productionAccess": False, "charts": outcomes, "publicChartSHA256": used, "preservedChartObjectUIDs": len(original),
            "unchangedBinding": {"claim": bound[0], "uidPreserved": True, "volume": bound[2],
                                 "claimTemplateSize": "10Gi", "seededBoundCapacity": "50Gi"},
            "excluded": ["pod startup and readiness", "CSI binding and disk I/O", "engine version changes",
                         "production impersonation and policies", "version and trace probes"],
            "deniedRequests": denials, "admissionDecisions": decisions,
            "apiRequests": [{"verb": v, "resource": r, "subresource": s, "status": c, "count": n}
                            for (v, r, s, c), n in sorted(requests.items())]}
