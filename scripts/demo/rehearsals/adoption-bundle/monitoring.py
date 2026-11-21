#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Exercise the private bundle boundary with a real chart and a synthetic observer."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from cryptography.fernet import Fernet
import yaml

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "scripts/demo"))
import adoption_bundle as bundle


RELEASE = "default/prometheus"
PREFIX = "releases/" + RELEASE + "/"
CLUSTER_KINDS = {"CustomResourceDefinition", "ClusterRole", "ClusterRoleBinding",
                 "MutatingWebhookConfiguration", "ValidatingWebhookConfiguration"}


def render(chart, values, version, approved=None):
    with tempfile.TemporaryDirectory() as name:
        directory = Path(name)
        (directory / "chart.tgz").write_bytes(chart)
        (directory / "values.yaml").write_bytes(values)
        env = dict(os.environ, KUBECONFIG=str(directory / "no-kubeconfig"),
                   HELM_CACHE_HOME=str(directory / "helm-cache"))
        command = ["helm", "template", "prometheus", str(directory / "chart.tgz"), "--namespace", "default",
                   "--kube-version", version, "--api-versions", "v1", "--include-crds", "-f", str(directory / "values.yaml")]
        if approved is not None:
            path = directory / "approved.yaml"
            path.write_bytes(approved)
            path.chmod(0o600)
            env.pop("HELM_NO_PLUGINS", None)
            env.update(HELM_PLUGINS=str(ROOT / "scripts/demo/helm-plugins"), JAEGER_APPROVED_MANIFEST=str(path))
            command.extend(["--post-renderer", "verify-frozen-manifests"])
        else:
            env["HELM_NO_PLUGINS"] = "1"
        # Verification failures must not export any partial Helm output or Secret data.
        return subprocess.run(command, env=env, capture_output=True, check=True).stdout


def exercise(checkout, chart, temp):
    commit = subprocess.check_output(["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    lock = (checkout / "scripts/demo/helmfile-tools.lock.json").read_bytes()
    context = {"kubernetesVersion": json.loads(lock)["kubernetesSchema"]["version"], "apiVersions":["v1"]}
    releases = yaml.safe_load((checkout / "helmfile.yaml").read_text())["releases"]
    release, = [r for r in releases if r["namespace"] == "default" and r["name"] == "prometheus"]
    matrix = json.loads((checkout / "docs/all-five-chart-access.json").read_bytes())
    # The reviewed matrix lists every templated object of the monitoring release, hooks included.
    reviewed_objects = sum((r["component"], r["release"]) == ("oci-monitoring", "prometheus") for r in matrix["records"])
    values = yaml.safe_load((checkout / "examples/oci/monitoring-values.yaml").read_bytes())
    values.setdefault("crds", {}).setdefault("upgradeJob", {}).update(enabled=True, forceConflicts=True)
    values.setdefault("grafana", {})["adminPassword"] = "synthetic-rehearsal-password"
    values = yaml.safe_dump(values).encode()
    manifests = render(chart, values, context["kubernetesVersion"])
    objects = [obj for obj in yaml.load_all(manifests, Loader=bundle.ManifestLoader) if obj]
    observed = set()
    for obj in objects:
        kind, meta = obj["kind"], obj["metadata"]
        ns = meta.get("namespace") or ("-" if kind in CLUSTER_KINDS else "default")
        observed.add(":".join([obj["apiVersion"], kind, ns, meta["name"]]))
    target = {"status":"deployed", "revision":7, "recordSHA256":bundle.digest(b"synthetic Helm history")}
    row = {"id":RELEASE, "needs":release.get("needs", []), "observe":sorted(observed),
           "chart":{"name":"kube-prometheus-stack", "version":release["version"],
                    "source":release["chart"], "sha256":bundle.digest(chart)},
           "helm":{"operation":"upgrade", "wait":"legacy", "atomic":False, "skipCRDs":False,
                   "historyMax":10, "createNamespace":False, "timeoutSeconds":release["timeout"]},
           "checks":["grafana-ready", "jaeger-scrape-target-up"], "rollback":{
               "strategy":"helm-rollback", "revision":7, "recordSHA256":target["recordSHA256"]}}
    plan = {"format":bundle.FORMAT, "configurationSHA":commit, "mode":"adoption", "renderContext":context,
            "selector":{"stack":"oci", "component":"monitoring"}, "requested":[RELEASE], "releases":[row], "images":{}}
    live = {"cluster":"credential-free-fixture", "healthy":True, "capabilitiesSHA256":bundle.digest(bundle.canonical(context)),
            "releases":{RELEASE:target}, "rollbackTargets":{RELEASE:target},
            "objects":{k:{"uid":"synthetic", "stateSHA256":bundle.digest(k.encode())} for k in observed}}
    inputs, output = temp / "inputs", temp / "sealed"
    inputs.mkdir(mode=0o700)
    output.mkdir(mode=0o700)
    (inputs / PREFIX).mkdir(parents=True)
    for filename, data in {"plan.json":bundle.canonical(plan), "live.json":bundle.canonical(live),
                           "tools.lock.json":lock, PREFIX+"chart.tgz":chart, PREFIX+"values.yaml":values,
                           PREFIX+"rendered.yaml":manifests}.items():
        (inputs / filename).write_bytes(data)

    cipher = Fernet(Fernet.generate_key())
    approved = bundle.build(inputs, output, cipher, commit, checkout)["bundleSHA256"]
    sealed = output / (approved + ".fernet")
    def rerender(plan, files):
        return {RELEASE:render(files[PREFIX+"chart.tgz"], files[PREFIX+"values.yaml"],
                               plan["renderContext"]["kubernetesVersion"])}
    verified = {}
    def apply(row, method, files):
        assert row["id"] == RELEASE and method == "apply"
        if files["chart.tgz"] != chart or files["rendered.yaml"] != manifests:
            raise AssertionError("Executor received unexpected bytes")
        expected = files["postrender.yaml"]
        ordinary = [o for o in yaml.load_all(expected, Loader=bundle.ManifestLoader) if o]
        static = len(objects) - len(ordinary)
        if (static, len(ordinary)) != (10, reviewed_objects):
            raise AssertionError("Unexpected archived CRDs or transaction resource count")
        protected = render(chart, values, context["kubernetesVersion"], approved=expected)
        if sum(o is not None for o in yaml.load_all(protected, Loader=bundle.ManifestLoader)) != len(objects):
            raise AssertionError("Verified Helm output differs in resource count")
        try:
            render(chart, values, context["kubernetesVersion"], approved=manifests)
        except subprocess.CalledProcessError:
            pass
        else:
            raise AssertionError("Post-renderer unexpectedly received static CRDs")
        verified.update(postRenderedObjects=len(ordinary), staticCRDsVerified=static)
    def consume():
        return bundle.consume(sealed, cipher, approved, checkout, commit, rerender, lambda scope:live,
                              apply, lambda stack: stack == "oci")
    assert consume()["verifiedStacks"] == ["oci"]
    target["revision"] += 1
    try:
        consume()
    except bundle.BundleError as error:
        if "drifted" not in str(error):
            raise
    else:
        raise AssertionError("Changed revision reached the executor")
    print(json.dumps({"scope":"credential-free", "configurationSHA":commit, "bundleSHA256":approved,
                      "renderedObjects":len(objects), "secretObjects":sum(obj["kind"]=="Secret" for obj in objects),
                      **verified,
                      "exactFrozenRenderVerified":True, "driftRefused":True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chart", type=Path, help="Reviewed kube-prometheus-stack chart archive")
    parser.add_argument("--checkout", type=Path, default=ROOT)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as name:
        exercise(args.checkout.resolve(), args.chart.read_bytes(), Path(name))
