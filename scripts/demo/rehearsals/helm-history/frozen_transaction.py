# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Exercise the production scripts-v1 Helm command on the private envtest fixture."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import scheduled_adapter as a


def exercise(root, config, kubectl, chart, writer):
    # Run the distributed Helm binary and transaction plugin as the restricted
    # fixture user. Helm Secret data and the short-lived token stay under root.
    cfg = json.loads(config.read_bytes())
    cfg["users"][0]["user"].update({"as": writer, "as-groups": ["system:authenticated"]})
    private = root / "frozen-writer.kubeconfig"
    private.write_text(json.dumps(cfg))
    package = root / "scheduled-chart"
    shutil.copytree(chart, package)
    (package / "Chart.yaml").write_text("apiVersion: v2\nname: opensearch\nversion: 0.1.0\n")
    with patch.dict(os.environ, {"KUBECONFIG": str(private), "PATH": str(Path(kubectl).parent) + os.pathsep + os.environ["PATH"]}):
        a.command(["helm", "package", str(package), "--destination", str(root)])
        chart_bytes = (root / "opensearch-0.1.0.tgz").read_bytes()
        initial, _, _ = a.history("opensearch")
        values = a.encode({"marker": "stable"})
        preview = a.helm_transaction("opensearch", chart_bytes, values)
        expected = a.release_docs(preview)
        assert preview["version"] == initial[0]["revision"] + 1 and expected
        altered = json.loads(a.encode(expected))
        altered[0]["metadata"]["labels"] = {"deliberate": "difference"}
        try:
            a.helm_transaction("opensearch", chart_bytes, values, expected=altered, dry=False)
        except a.AdapterError:
            pass
        else:
            raise AssertionError("The post-renderer accepted drift")
        assert a.history("opensearch")[0] == initial
        a.helm_transaction("opensearch", chart_bytes, values, expected=expected, dry=False)
        after, stored, _ = a.history("opensearch")
        assert after[0]["revision"] == initial[0]["revision"] + 1 and after[0]["status"] == "deployed"
        assert a.index(a.release_docs(stored), "opensearch") == a.index(expected, "opensearch")
        try:
            a.helm_transaction("opensearch", chart_bytes, a.encode({"marker": "immutable-change"}), dry=False)
        except a.AdapterError:
            pass
        else:
            raise AssertionError("The private API allowed the immutable fixture change")
        failure, _, rollback = a.history("opensearch")
        assert failure[0]["status"] == "failed" and rollback["version"] == after[0]["revision"]
    return {"identity": writer, "immutableChartAndValues": True, "serverDryRunMatchedStoredUpgrade": True,
            "changedManifestBlockedBeforeHelmHistory": True, "priorRevision": initial[0]["revision"],
            "deployedRevision": after[0]["revision"], "failedRevision": failure[0]["revision"], "recoveryTarget": rollback["version"],
            "workloads": "synthetic ConfigMap", "realNetworkHealth": False}


def monitoring_lookup(root, config, kubectl, kube, apply, package):
    from base64 import b64encode
    import secrets
    package = package.read_bytes()
    archive = json.loads((a.ROOT / "docs/all-five-chart-access.json").read_bytes())["chartArchiveSHA256"]
    assert a.sha(package) == archive["kube-prometheus-stack-82.10.4.tgz"]
    path = root / "monitoring-lookup.tgz"
    path.write_bytes(package)
    crds = a.docs(a.command(["helm", "show", "crds", path]))
    assert len(crds) == 10
    for crd in crds:
        kube(["apply", "--server-side", "-f", "-"], body=json.dumps(crd))
    kube(["wait", "crd", "--all", "--for=condition=Established", "--timeout=40s"])
    marker = b64encode(secrets.token_bytes(32)).decode()
    apply({"apiVersion": "v1", "kind": "Secret", "type": "Opaque", "metadata": {"name": "prometheus-grafana", "namespace": "default",
           "annotations": {"meta.helm.sh/release-name": "prometheus", "meta.helm.sh/release-namespace": "default"},
           "labels": {"app.kubernetes.io/managed-by": "Helm"}}, "data": {"admin-password": marker}})
    group = "rbac.authorization.k8s.io"
    user = "fixture-render-reader"
    apply({"apiVersion": group + "/v1", "kind": "ClusterRole", "metadata": {"name": user},
           "rules": [{"apiGroups": ["*"], "resources": ["*"], "verbs": ["get", "list"]}]})
    apply({"apiVersion": group + "/v1", "kind": "ClusterRoleBinding", "metadata": {"name": user},
           "roleRef": {"apiGroup": group, "kind": "ClusterRole", "name": user}, "subjects": [{"apiGroup": group, "kind": "User", "name": user}]})
    cfg = json.loads(config.read_bytes())
    cfg["users"][0]["user"].update({"as": user, "as-groups": ["system:authenticated"]})
    private = root / "monitoring-reader.kubeconfig"
    private.write_text(json.dumps(cfg))
    with patch.dict(os.environ, {"KUBECONFIG": str(private), "PATH": str(Path(kubectl).parent) + os.pathsep + os.environ["PATH"]}):
        denied = subprocess.run([kubectl, "auth", "can-i", "patch", "secrets", "-n", "default"], capture_output=True)
        assert denied.returncode == 1 and denied.stdout.strip() == b"no"
        values = a.encode(a.values_for("oci-monitoring", a.ROOT, {}))
        first = a.helm_transaction("oci-monitoring", package, values)
        rendered = a.release_docs(first)
        confirmed = a.release_docs(a.helm_transaction("oci-monitoring", package, values, expected=rendered))
        assert a.index(rendered, "default") == a.index(confirmed, "default")
        grafana = next(o for o in rendered if o["kind"] == "Secret" and o["metadata"]["name"] == "prometheus-grafana")
        assert grafana["data"]["admin-password"] == marker
        assert len(a.release_docs(first)) == 129 and len(first["hooks"]) == 12
        assert a.history("oci-monitoring")[0][0]["revision"] == 6
    return {"reader": user, "secretPatchDenied": True, "chartSHA256": a.sha(package), "renderedObjects": len(rendered),
            "hookObjects": len(first["hooks"]), "seededCRDs": len(crds), "grafanaLookupMatchedPrivateFixture": True,
            "unchangedHelmRevision": 6, "frozenPostRendererPassed": True}
