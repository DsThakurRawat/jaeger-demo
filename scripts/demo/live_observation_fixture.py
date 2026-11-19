# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Private envtest fixture entered only from rehearse-six-release-history.py."""

import json
import secrets

from live_observation import canonical, history, observe, reader


def exercise(root, config, kubectl, kube, helm, chart, apply):
    name = "history-observer"
    rbac = "rbac.authorization.k8s.io"
    subject = {"apiGroup": rbac, "kind": "User", "name": name}
    def grant(cluster, rules):
        kind = "ClusterRole" if cluster else "Role"
        meta = {"name": name} if cluster else {"name": name, "namespace": "default"}
        apply({"apiVersion": rbac + "/v1", "kind": kind, "metadata": meta, "rules": rules})
        apply({"apiVersion": rbac + "/v1", "kind": kind + "Binding", "metadata": meta,
               "roleRef": {"apiGroup": rbac, "kind": kind, "name": name}, "subjects": [subject]})
    grant(True, [{"apiGroups": [""], "resources": ["namespaces"], "resourceNames": ["kube-system"], "verbs": ["get"]}])
    names = ["fixture-sensitive"] + [f"sh.helm.release.v1.prometheus.v{n}" for n in (4, 5, 6, 7, 8)]
    grant(False, [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["list"]},
                  {"apiGroups": [""], "resources": ["secrets"], "resourceNames": names, "verbs": ["get"]},
                  {"apiGroups": ["apps"], "resources": ["deployments"], "resourceNames": [name], "verbs": ["get"]}])
    config_data = json.loads(config.read_text())
    identity = config_data["users"][0]["user"]
    identity.update({"as": name, "as-groups": ["system:authenticated"]})
    private = root / "readonly-observer.kubeconfig"
    private.write_text(json.dumps(config_data))
    forbidden = kube(["auth", "can-i", "patch", "deployments", "--as", name, "--as-group", "system:authenticated"], check=False)
    assert forbidden.stdout.strip() == "no"
    value = secrets.token_hex(32)
    apply({"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "fixture-sensitive", "namespace": "default"},
           "stringData": {"value": value}})
    apply({"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": "default"},
           "spec": {"replicas": 0, "selector": {"matchLabels": {"fixture": name}},
                    "template": {"metadata": {"labels": {"fixture": name}},
                                 "spec": {"containers": [{"name": "fixture", "image": "fixture.invalid/unused"}]}}}})
    dep = f"apps/v1:Deployment:default:{name}"
    secret = "v1:Secret:default:fixture-sensitive"
    scope = {"releases": ["default/prometheus", "default/demo-resources"], "objects": [dep, secret], "imageStacks": []}
    # This private API has no workload controller or public endpoints. The live
    # hosted health checker has separate tests. Its fixture callback receives
    # the selected stack and permits only this exact local Release observation.
    def fixture_health(stacks):
        return stacks == ["oci"]
    def refused(fn):
        try:
            fn()
        except ValueError:
            return True
        raise AssertionError("Expected the observation to stop")
    with reader(private, "fixture", kubectl) as get:
        first, caps = observe(scope, get, fixture_health)
        assert value not in canonical(first).decode() and "stringData" not in canonical(first).decode()
        assert caps["kubernetesVersion"].startswith("v1.") and "apps/v1/Scale" in caps["apiVersions"]
        assert first["releases"]["default/prometheus"] == first["rollbackTargets"]["default/prometheus"]
        assert first["releases"]["default/prometheus"]["revision"] == 6
        assert first["rollbackTargets"]["default/demo-resources"] is None
        scope["cluster"] = first["cluster"]
        scope["rollbackRevisions"] = {"default/prometheus": 6, "default/demo-resources": 0}
        patch = ["patch", "deployment", name, "-n", "default", "--type=merge"]
        kube([*patch, "--subresource=status", "-p", '{"status":{"observedGeneration":1,"replicas":1}}'])
        assert first == observe(scope, get, fixture_health)[0]
        kube([*patch, "-p", '{"spec":{"replicas":1}}'])
        changed = observe(scope, get, fixture_health)[0]
        assert first["objects"][dep]["uid"] == changed["objects"][dep]["uid"]
        assert first["objects"][dep]["stateSHA256"] != changed["objects"][dep]["stateSHA256"]
        kube(["patch", "secret", "fixture-sensitive", "--type=merge", "-p", '{"stringData":{"value":"changed"}}'])
        assert changed["objects"][secret]["stateSHA256"] != observe(scope, get, fixture_health)[0]["objects"][secret]["stateSHA256"]
        kube(["delete", "secret", "fixture-sensitive"])
        assert observe(scope, get, fixture_health)[0]["objects"][secret] is None
        # Genuine non-atomic failures leave revisions 7 and 8 failed, with 6
        # the highest successful revision. Verify both failures by named GET.
        for _ in range(2):
            run = helm(["upgrade", "prometheus", chart, "--history-max", "10", "--set-string", "marker=incompatible"], "default", check=False)
            assert run.returncode and "immutable" in run.stderr.lower()
        current, target = history(get, "default/prometheus", 6)
        assert (current["revision"], current["status"], target["revision"], target["status"]) == (8, "failed", 6, "deployed")
        assert target == first["releases"]["default/prometheus"]
        refused(lambda: history(get, "default/prometheus", 4))
        # The fixture's admin removes only the synthetic intervening history.
        kube(["delete", "secret", "sh.helm.release.v1.prometheus.v7", "-n", "default"])
        refused(lambda: history(get, "default/prometheus", 6))
        refused(lambda: observe(dict(scope, cluster="foreign"), get, fixture_health))
        refused(lambda: get("/api/v1/namespaces/default/secrets/sh.helm.release.v1.jaeger.v6"))
    return {"observer": name, "writeDenied": True, "statusOnlyStable": True, "specDrift": True,
            "secretValueDrift": True, "missingObject": True, "helmRevisions": [6, 7, 8], "rollbackRevision": 6,
            "olderRollbackRejected": True, "missingInterveningRecordRejected": True,
            "differentClusterRejected": True, "unrelatedNamedSecretReadDenied": True,
            "healthProbe": "private fixture callback", "externalAccess": False}
