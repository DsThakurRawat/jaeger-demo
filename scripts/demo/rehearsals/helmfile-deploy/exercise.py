#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Rehearse the Helmfile deploy driver on disposable Kubernetes as the bound deployer user.

The driver (scripts/demo/helmfile_deploy.py) runs unchanged against six synthetic application
releases named like the real ones, in the four real namespaces, and the real demo-resources chart
with its real source values. The image resolution, the Jaeger configuration gate, the registry
lookup behind the live image read, the public health check and the trace check are stubs, because
they need public registries, a container runtime and the public demo. It proves the acceptance
cases of #73: a healthy no-op creates no revision, a failed release with an empty diff is retried
with sync, a pending release blocks before any write, failed OCI verification stops before OTel,
and an incompatible image fails before the first write. A live-mode pass then reads the images of
fixture Jaeger and HotROD Deployments and runs the production rollout, storage and Certificate
checks.
"""

import argparse
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import companion_values  # noqa: E402
import deploy  # noqa: E402
import deployer_access  # noqa: E402
import helmfile_deploy  # noqa: E402
import release_phases  # noqa: E402

CONTEXT = "kind-jaeger-demo-operations-fixture"
# A rehearsal identity bound to the committed deployer roles. It matches the OCI user OCID pattern.
USER = "ocid1.user.oc1..aaaarehearsaloperations"
REPO = helmfile_deploy.ROOT
APPLICATIONS = ["default/prometheus", "default/jaeger", "opensearch/opensearch", "opensearch/opensearch-dashboards",
                "jaeger/jaeger", "otel-demo/otel-demo"]
COMPANIONS = [ns + "/" + release_phases.COMPANION for ns in release_phases.NAMESPACES]
CHART = """apiVersion: v1
kind: ConfigMap
metadata:
  name: {{ .Release.Name }}-fixture
data:
  release: {{ .Release.Name | quote }}
  {{- with .Values.allInOne }}
  jaeger: {{ printf "%s:%s %s" .image.repository .image.tag .image.pullPolicy | quote }}
  {{- end }}
  {{- with .Values.hotrod }}
  hotrod: {{ printf "%s:%s %s" .image.repository .image.tag .image.pullPolicy | quote }}
  {{- end }}
"""
# Like the Grafana admin password of the monitoring chart: kept through lookup, generated only on a
# first install. A diff that renders without lookup sees a new password and a new checksum on every
# run, so the healthy no-op case fails if that ever regresses.
SECRET = """{{- if eq .Release.Name "prometheus" }}
{{- $existing := lookup "v1" "Secret" .Release.Namespace "prometheus-fixture-admin" }}
apiVersion: v1
kind: Secret
metadata:
  name: prometheus-fixture-admin
data:
  admin-password: {{ if $existing }}{{ index $existing.data "admin-password" }}{{ else }}{{ randAlphaNum 40 | b64enc }}{{ end }}
{{- end }}
"""
WORKLOAD = """{{- if eq .Release.Name "prometheus" }}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: prometheus-grafana
spec:
  replicas: 1
  selector:
    matchLabels: {app: prometheus-grafana-fixture}
  template:
    metadata:
      labels: {app: prometheus-grafana-fixture}
      annotations:
        checksum/secret: {{ include (print $.Template.BasePath "/secret.yaml") . | sha256sum }}
    spec:
      containers:
        - name: grafana
          image: registry.k8s.io/pause:3.10
{{- end }}
"""
PAUSE = "registry.k8s.io/pause:3.10"
# A published snapshot commit of both images. The live-mode pass runs them as the live references.
LIVE_TAG = "719b98487c3ee3ba9062e1527e6f8153a0c801f2"
LIVE_REPOS = {"jaeger": "jaegertracing/jaeger-snapshot", "jaeger-hotrod": "jaegertracing/example-hotrod-snapshot"}
CERTIFICATE_CRD = {
    "apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
    "metadata": {"name": "certificates.cert-manager.io"},
    "spec": {"group": "cert-manager.io", "scope": "Namespaced",
             "names": {"kind": "Certificate", "plural": "certificates", "singular": "certificate",
                       "listKind": "CertificateList"},
             "versions": [{"name": "v1", "served": True, "storage": True, "schema": {"openAPIV3Schema": {
                 "type": "object", "x-kubernetes-preserve-unknown-fields": True}}}]}}


def deployment(ns, name, image, container=None):
    labels = {"app": name + "-fixture"}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns},
            "spec": {"replicas": 1, "selector": {"matchLabels": labels}, "template": {
                "metadata": {"labels": labels},
                "spec": {"containers": [{"name": container or name, "image": image, "imagePullPolicy": "IfNotPresent"}]}}}}


def workloads():
    """What the production stack checks wait for, outside the synthetic charts: the live Jaeger and
    HotROD images, and placeholders for the OpenSearch, Dashboards and OTel demo workloads."""
    items = [deployment(ns, name, f"docker.io/{repo}:{LIVE_TAG}")
             for ns in ("default", "jaeger") for name, repo in LIVE_REPOS.items()]
    items += [deployment("opensearch", "opensearch-dashboards", PAUSE),
              deployment("otel-demo", "frontend", PAUSE), deployment("otel-demo", "load-generator", PAUSE)]
    agent = {"app": "otel-collector-agent-fixture"}
    items.append({"apiVersion": "apps/v1", "kind": "DaemonSet", "metadata": {"name": "otel-collector-agent",
                                                                            "namespace": "otel-demo"},
                  "spec": {"selector": {"matchLabels": agent}, "template": {"metadata": {"labels": agent},
                           "spec": {"containers": [{"name": "agent", "image": PAUSE}]}}}})
    single = {"app": "opensearch-cluster-single-fixture"}
    items.append({"apiVersion": "apps/v1", "kind": "StatefulSet",
                  "metadata": {"name": "opensearch-cluster-single", "namespace": "opensearch"},
                  "spec": {"serviceName": "opensearch-cluster-single", "replicas": 1, "selector": {"matchLabels": single},
                           "template": {"metadata": {"labels": single}, "spec": {"containers": [{
                               "name": "opensearch", "image": PAUSE, "volumeMounts": [
                                   {"name": "opensearch-cluster-single", "mountPath": "/usr/share/opensearch/data"}]}]}},
                           "volumeClaimTemplates": [{"metadata": {"name": "opensearch-cluster-single"}, "spec": {
                               "accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "1Mi"}}}}]}})
    return {"apiVersion": "v1", "kind": "List", "items": items}


def certificate(ns, name, ready="True"):
    return {"apiVersion": "cert-manager.io/v1", "kind": "Certificate", "metadata": {"name": name, "namespace": ns},
            "spec": {"secretName": name}, "status": {"conditions": [{"type": "Ready", "status": ready}]}}


def fixture_state():
    """helmfile.yaml with the real release identities, labels, needs and atomic settings and one local chart."""
    def release(name, namespace, stack, component, needs=(), atomic=False):
        entry = {"name": name, "namespace": namespace, "labels": {"stack": stack, "component": component},
                 "chart": "./fixture-charts/app", "timeout": 180}
        if needs:
            entry["needs"] = list(needs)
        if atomic:
            entry["atomic"] = True
            entry["values"] = ["helmfile/jaeger-images.yaml.gotmpl"]
        return entry

    real = {r["namespace"] + "/" + r["name"]: r for r in yaml.safe_load((REPO / "helmfile.yaml").read_text())["releases"]}
    releases = [release("prometheus", "default", "oci", "monitoring"),
                release("jaeger", "default", "oci", "jaeger", ["default/prometheus"], atomic=True),
                release("opensearch", "opensearch", "otel", "opensearch"),
                release("opensearch-dashboards", "opensearch", "otel", "opensearch", ["opensearch/opensearch"]),
                release("jaeger", "jaeger", "otel", "jaeger", ["opensearch/opensearch"], atomic=True),
                release("otel-demo", "otel-demo", "otel", "otel-demo", ["jaeger/jaeger"])]
    for entry in releases:
        key = entry["namespace"] + "/" + entry["name"]
        # The fixture keeps every property of the real state that orders or gates a release.
        assert {k: real[key].get(k) for k in ("labels", "needs", "atomic")} == \
            {k: entry.get(k) for k in ("labels", "needs", "atomic")}, key
    return {"helmDefaults": yaml.safe_load((REPO / "helmfile.yaml").read_text())["helmDefaults"], "releases": releases}


def selection(head, tag):
    repos = {"jaeger": "jaegertracing/jaeger-snapshot", "hotrod": "jaegertracing/example-hotrod-snapshot"}
    digest = "sha256:" + hashlib.sha256(tag.encode()).hexdigest()
    sources = {name: {"repository": repo, "tag": tag, "sourceCommit": tag, "digest": digest,
                      "platformManifest": digest, "reference": f"docker.io/{repo}:{tag}@{digest}"}
               for name, repo in repos.items()}
    images = {name: {"repository": repo, "tag": tag + "@" + digest, "pullPolicy": "Always"} for name, repo in repos.items()}
    return {"mode": "promotion", "configCommit": head, "coreMain": "0" * 40, "platform": "linux/amd64",
            "sources": sources, "images": {"oci": images, "otel": images}}


def receipt(data, digest, renders, runtime):
    # The stub gate accepts a render only when it carries the selected references, as the real one does.
    chosen = json.loads(data)
    for stack, render in renders.items():
        for name, image in chosen["images"][stack].items():
            assert f"{image['repository']}:{image['tag']} Always".encode() in render, (stack, name)
    return {"mode": "promotion", "configCommit": chosen["configCommit"], "selectionSHA256": digest,
            "checks": {stack: {"result": "passed", "renderSHA256": hashlib.sha256(r).hexdigest()}
                       for stack, r in renders.items()}}


class Checks:
    """Stub stack checks. The admin confirms each release of the stack is deployed."""

    def __init__(self, helm):
        self.helm, self.calls, self.fail = helm, [], set()

    def prepare(self, stacks):
        self.calls.append(("prepare", tuple(sorted(stacks))))

    def verify(self, stack):
        self.calls.append(("verify", stack))
        if stack in self.fail:
            raise RuntimeError(f"fixture {stack} health unavailable")
        for key in APPLICATIONS:
            if release_phases.stack_of(key) == stack:
                ns, name = key.split("/")
                out = json.loads(subprocess.check_output(self.helm + ["-n", ns, "status", name, "-o", "json"]))
                assert out["info"]["status"] == "deployed", key
        return {"fixture": "healthy"}


def exercise(cfg, context):
    ctl = ["kubectl", "--kubeconfig", str(cfg), "--context", context]
    # The admin inspects with the pinned Helm directly, so its reads stay out of the deployer's Helm log.
    real_helm = shutil.which("helm")
    admin_helm = [real_helm, "--kubeconfig", str(cfg), "--kube-context", context]
    view = json.loads(subprocess.check_output(ctl + ["config", "view", "--minify", "-o", "json"]))
    if urlsplit(view["clusters"][0]["cluster"]["server"]).hostname != "127.0.0.1":
        raise ValueError("A disposable loopback cluster is required")
    present = set(subprocess.check_output(ctl + ["get", "ns", "-o", "jsonpath={.items[*].metadata.name}"], text=True).split())
    if present & set(release_phases.NAMESPACES[1:]):
        raise ValueError("Use a new fixture without the release namespaces")
    for ns in release_phases.NAMESPACES[1:]:
        subprocess.run(ctl + ["create", "namespace", ns], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(ctl + ["apply", "-f", str(deployer_access.ROLES)], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(ctl + ["apply", "-f", "-"], input=json.dumps(deployer_access.bindings(USER)).encode(),
                   check=True, stdout=subprocess.DEVNULL)
    # The script engine's fingerprint record exists in production before any Helmfile run.
    subprocess.run(ctl + ["-n", "default", "create", "configmap", deploy.STATE,
                          *[f"--from-literal={name}=stale" for name in deploy.COMPONENTS]], check=True, stdout=subprocess.DEVNULL)

    scratch = tempfile.TemporaryDirectory()
    work = Path(scratch.name)
    config = yaml.safe_load(Path(cfg).read_text())
    config["current-context"] = context
    for entry in config["users"]:
        entry["user"]["as"] = USER
        entry["user"]["as-groups"] = ["system:authenticated"]
    deployer = work / "deployer.kubeconfig"
    with open(os.open(deployer, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as f:
        yaml.safe_dump(config, f)
    who = subprocess.run(["kubectl", "--kubeconfig", str(deployer), "auth", "whoami", "-o",
                          "jsonpath={.status.userInfo.username}"], capture_output=True, text=True, check=True)
    assert who.stdout.strip() == USER, who.stdout
    # Every driver command, Helm and Helmfile included, runs as the deployer through this kubeconfig.
    os.environ["KUBECONFIG"] = str(deployer)

    # Log every Helm invocation of the driver, including those Helmfile makes, then run the pinned Helm.
    shim = work / "shim"
    shim.mkdir()
    helm_log = work / "helm.log"
    (shim / "helm").write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{helm_log}"\nexec "{real_helm}" "$@"\n')
    (shim / "helm").chmod(0o755)
    os.environ["PATH"] = str(shim) + os.pathsep + os.environ["PATH"]

    # The configuration root: the real chart, companion sources and fingerprint inputs, and a fixture state.
    root = work / "root"
    root.mkdir()
    for name in ("examples", "scripts", "charts", "helmfile-resources.yaml.gotmpl"):
        (root / name).symlink_to(REPO / name)
    (root / "helmfile").mkdir()
    (root / "helmfile/jaeger-images.yaml.gotmpl").symlink_to(REPO / "helmfile/jaeger-images.yaml.gotmpl")
    (root / "helmfile/images.yaml").write_text("mode: digest\n")
    (root / "fixture-charts/app/templates").mkdir(parents=True)
    (root / "fixture-charts/app/Chart.yaml").write_text("apiVersion: v2\nname: app-fixture\nversion: 1.0.0\n")
    (root / "fixture-charts/app/templates/config.yaml").write_text(CHART)
    (root / "fixture-charts/app/templates/secret.yaml").write_text(SECRET)
    (root / "fixture-charts/app/templates/workload.yaml").write_text(WORKLOAD)
    state = yaml.safe_dump(fixture_state())
    (root / "helmfile.yaml").write_text(state)
    (root / helmfile_deploy.APPLICATIONS).write_text(state)
    git = ["git", "-C", str(root), "-c", "user.name=rehearsal", "-c", "user.email=rehearsal@example.invalid"]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "fixture"], check=True)
    head = subprocess.check_output(git + ["rev-parse", "HEAD"], text=True).strip()
    summary = work / "summary.md"
    base = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": head,
            "EXPECTED_CONFIG_SHA": head, "DEPLOY_ENGINE": "helmfile", "HELMFILE_OPERATION": "apply",
            "COMPANION_NAMESPACE": "none", "GITHUB_RUN_ID": "rehearsal", "GITHUB_STEP_SUMMARY": str(summary),
            "MAIN_SHA": "0" * 40}

    commands = []

    def run(args, **kwargs):
        commands.append([str(a) for a in args])
        return helmfile_deploy.run_command(args, **kwargs)

    def writes():
        return [c for c in commands if (c[0] == "helmfile" and c[c.index("--skip-deps") + 1] in ("apply", "sync"))
                or c[:2] == ["helm", "install"] or (c[0] == "kubectl" and ({"patch", "restart"} & set(c)))]

    def charts(path):
        return {"charts": {"app-fixture": hashlib.sha256((path / "fixture-charts/app/templates/config.yaml").read_bytes()).hexdigest()}}

    checks = Checks(admin_helm)

    def images(tag, gate=receipt):
        def resolve(event, overrides, core_main=None):
            # The driver selects for the upstream commit the workflow resolved, MAIN_SHA.
            assert core_main == base["MAIN_SHA"], core_main
            return selection(head, tag)

        return helmfile_deploy.images(dict(base), root=root, run=run, resolve=resolve, validate=gate)

    def reconcile(**env):
        commands.clear()
        checks.calls.clear()
        helm_log.write_text("")
        return helmfile_deploy.reconcile(dict(base, **env), root=root, run=run, checks=checks,
                                         verify_charts=charts, sleep=lambda s: None)

    def refused(message, **env):
        try:
            reconcile(**env)
        except ValueError as error:
            assert message in str(error), str(error)
        else:
            raise AssertionError(f"Reconcile did not refuse: {message}")
        return json.loads((root / helmfile_deploy.RECORD).read_text())

    def revisions():
        found = {}
        for key in APPLICATIONS + COMPANIONS:
            ns, name = key.split("/")
            out = subprocess.run(admin_helm + ["-n", ns, "history", name, "--max", "1", "-o", "json"],
                                 capture_output=True, text=True)
            found[key] = None if out.returncode else (json.loads(out.stdout)[0]["revision"], json.loads(out.stdout)[0]["status"])
        return found

    def force(key, status):
        # Build a state an interrupted transaction leaves behind. Only the fixture's release record changes.
        ns, name = key.split("/")
        revision = json.loads(subprocess.check_output(admin_helm + ["-n", ns, "status", name, "-o", "json"]))["version"]
        secret = json.loads(subprocess.check_output(ctl + ["-n", ns, "get", "secret", f"sh.helm.release.v1.{name}.v{revision}",
                                                           "-o", "json"]))
        payload = json.loads(gzip.decompress(base64.b64decode(base64.b64decode(secret["data"]["release"]))))
        payload["info"]["status"] = status
        secret["data"]["release"] = base64.b64encode(base64.b64encode(gzip.compress(json.dumps(payload).encode()))).decode()
        secret["metadata"]["labels"]["status"] = status
        subprocess.run(ctl + ["-n", ns, "replace", "-f", "-"], input=json.dumps(secret).encode(), check=True,
                       stdout=subprocess.DEVNULL)

    def upgrades():
        return [line.split() for line in helm_log.read_text().splitlines() if line.startswith("upgrade ")]

    def fingerprints():
        data = json.loads(subprocess.check_output(ctl + ["-n", "default", "get", "configmap", deploy.STATE, "-o", "json"]))["data"]
        return {name: data[name] for name in deploy.COMPONENTS}

    # Seed: the six applications as production holds them, installed once by the deployer.
    images("1" * 40)
    selection_file = root / helmfile_deploy.WORK / helmfile_deploy.SELECTION
    seed_sync = ["helmfile", "-f", str(root / helmfile_deploy.APPLICATIONS), "--state-values-file", str(selection_file),
                 "--skip-deps"]
    subprocess.run(seed_sync + ["sync", "--concurrency", "1"], cwd=root, check=True, stdout=subprocess.DEVNULL)
    # On a first install lookup finds nothing, so the Secret and the checksum of its separate render
    # each draw their own random value. Production's monitoring release is long past that: one upgrade
    # settles it, after which a render with lookup must match the release exactly.
    subprocess.run(seed_sync + ["-l", "namespace=default,name=prometheus", "sync"], cwd=root, check=True,
                   stdout=subprocess.DEVNULL)
    initial = {key: (2 if key == "default/prometheus" else 1, "deployed") for key in APPLICATIONS}
    assert {key: revisions()[key] for key in APPLICATIONS} == initial

    # The supplemental objects exist before adoption, as the scripts applied them. The reviewed
    # ownership patches then add the companion's Helm ownership metadata (docs/cutover.md).
    sources = companion_values.source_values()["companions"]

    def seed(ns, owned=True, skip=None):
        for obj in sources[ns]["objects"]:
            if obj["metadata"]["name"] == skip:
                continue
            obj = json.loads(json.dumps(obj))
            meta = obj["metadata"]
            meta["namespace"] = ns
            if owned:
                meta["labels"] = dict(meta.get("labels") or {}, **{"app.kubernetes.io/managed-by": "Helm"})
                meta["annotations"] = dict(meta.get("annotations") or {}, **{
                    "meta.helm.sh/release-name": release_phases.COMPANION, "meta.helm.sh/release-namespace": ns,
                    "helm.sh/resource-policy": "keep"})
            subprocess.run(ctl + ["apply", "-f", "-"], input=json.dumps(obj).encode(), check=True, stdout=subprocess.DEVNULL)

    def uids(ns):
        return {(o["kind"], o["metadata"]["name"]): o["metadata"]["uid"] for o in json.loads(subprocess.check_output(
            ctl + ["-n", ns, "get", "configmap,service,deployment,ingress", "-o", "json"]))["items"]
            if o["metadata"]["name"] in {s["metadata"]["name"] for s in sources[ns]["objects"]}}

    # A first install refuses an allowlisted object that is missing, so Helm can never create it with a
    # new UID, and one that does not carry the reviewed ownership.
    ingress = sources["otel-demo"]["objects"][0]["metadata"]["name"]
    refused("otel-demo/Ingress/" + ingress + " is missing", HELMFILE_OPERATION="install-companion",
            COMPANION_NAMESPACE="otel-demo")
    assert not writes()
    seed("otel-demo", owned=False)
    refused("lacks the reviewed demo-resources ownership", HELMFILE_OPERATION="install-companion",
            COMPANION_NAMESPACE="otel-demo")
    assert not writes() and revisions()["otel-demo/demo-resources"] is None

    # First companion installs: one manual dispatch per namespace, a plain helm install of an absent release
    # that adopts the existing objects. Adoption changes no pod template, so no generator restarts.
    for ns in release_phases.NAMESPACES:
        seed(ns)
        adopted = uids(ns)
        record = reconcile(HELMFILE_OPERATION="install-companion", COMPANION_NAMESPACE=ns)
        entry, = record["releases"]
        assert (entry["id"], entry["method"], entry["statusBefore"], entry["revisionAfter"], entry["statusAfter"]) == \
            (ns + "/demo-resources", "install", "absent", 1, "deployed"), entry
        assert not [c for c in commands if c[0] == "helmfile"] and record["fingerprints"] == []
        install, = [c for c in commands if c[:2] == ["helm", "install"]]
        assert "--take-ownership" not in install and "--rollback-on-failure=false" in install
        assert not [c for c in commands if "restart" in c] and record["generatorRestarts"] == []
        assert record["images"] is None and uids(ns) == adopted, "The install replaced an adopted object"
        expected = ["oci"] if ns == "default" else ["oci", "otel"]
        assert record["verifiedStacks"] == expected and [c for _, c in checks.calls[1:]] == expected, checks.calls
    again = refused("not absent", HELMFILE_OPERATION="install-companion", COMPANION_NAMESPACE="jaeger")
    assert again["outcome"] == "refused" and not writes()
    assert fingerprints() == dict.fromkeys(deploy.COMPONENTS, "stale")
    seeded = revisions()
    assert seeded == {**initial, **dict.fromkeys(COMPANIONS, (1, "deployed"))}, seeded

    # 1. A healthy no-op creates no revision and still verifies both stacks, on a scheduled run.
    record = reconcile(GITHUB_EVENT_NAME="schedule", EXPECTED_CONFIG_SHA="")
    assert revisions() == seeded, "A no-op apply added a revision"
    assert [r["id"] for r in record["releases"]] == [row["id"] for row in helmfile_deploy.release_rows(root)]
    assert all(r["method"] == "apply" and not r["changed"] for r in record["releases"])
    assert checks.calls == [("prepare", ("oci", "otel")), ("verify", "oci"), ("verify", "otel")]
    assert record["verifiedStacks"] == ["oci", "otel"] and not upgrades()
    # The prometheus fixture keeps its generated Secret through lookup; without it this diff would change.
    assert len([line for line in helm_log.read_text().splitlines() if line.startswith("diff upgrade")]) == 10
    # A digest-mode apply restarts each stack's generator before that stack's checks (parity with the scripts).
    restarts = [i for i, c in enumerate(commands) if "restart" in c]
    assert record["generatorRestarts"] == ["oci", "otel"] and [commands[i][2] for i in restarts] == ["default", "jaeger"]
    assert fingerprints() == {name: deploy.fingerprint(name, root) for name in deploy.COMPONENTS}
    assert "| default/jaeger | deployed | 1 | apply | 1 | no |" in summary.read_text()

    # 2. A failed release with an empty diff is retried with sync. Helmfile 1.8.4 maps a release's
    # atomic: true to Helm 4's --rollback-on-failure and adds nothing to a release without it.
    force("default/prometheus", "failed")
    force("jaeger/jaeger", "failed")
    helm_log.write_text("")
    commands.clear()
    # Helm's own rendered diff of both releases is empty. Helmfile reports a release that is not
    # deployed as changed, so the read-only driver diff names exactly these two.
    for key in ("default/prometheus", "jaeger/jaeger"):
        ns, name = key.split("/")
        stored = work / f"{ns}-{name}-values.yaml"
        stored.write_text(subprocess.check_output(admin_helm + ["-n", ns, "get", "values", name, "-o", "yaml"], text=True))
        empty = subprocess.run(admin_helm + ["-n", ns, "diff", "upgrade", name, str(root / "fixture-charts/app"),
                                             "--values", str(stored), "--reset-values", "--detailed-exitcode"],
                               stdout=subprocess.DEVNULL)
        assert empty.returncode == 0, f"{key} must have an empty rendered diff"
    planned = helmfile_deploy.diff(dict(base), root=root, run=run)
    assert planned == {row["id"]: "changes" if row["id"] in ("default/prometheus", "jaeger/jaeger") else "no changes"
                       for row in helmfile_deploy.release_rows(root)}, planned
    assert not writes() and not upgrades(), "diff wrote to the cluster"
    record = reconcile()
    methods = {r["id"]: (r["method"], r["revisionAfter"], r["changed"]) for r in record["releases"]}
    assert methods["default/prometheus"] == ("sync", 3, True) and methods["jaeger/jaeger"] == ("sync", 2, True), methods
    assert all(m == ("apply", 1, False) for k, m in methods.items() if k not in ("default/prometheus", "jaeger/jaeger"))
    flags = {line[2] + "/" + line[line.index("--namespace") + 1]: line for line in upgrades()}
    assert set(flags) == {"prometheus/default", "jaeger/jaeger"}, flags
    for line in flags.values():
        assert {"--install=false", "--reset-values", "--create-namespace=false"} <= set(line), line
        assert line.index("--install=false") > line.index("--install"), "--install=false must follow Helmfile's --install"
    assert "--rollback-on-failure" in flags["jaeger/jaeger"], flags["jaeger/jaeger"]
    assert not any("rollback-on-failure" in flag for flag in flags["prometheus/default"]), flags["prometheus/default"]
    current = revisions()
    assert current["default/prometheus"] == (3, "deployed") and current["jaeger/jaeger"] == (2, "deployed")

    # 3. A pending release blocks every release before any write.
    force("otel-demo/otel-demo", "pending-upgrade")
    record = refused("operator review")
    assert record["outcome"] == "refused" and not writes() and not upgrades()
    assert checks.calls == [("prepare", ("oci", "otel"))] and revisions() == {**current, "otel-demo/otel-demo": (1, "pending-upgrade")}
    force("otel-demo/otel-demo", "deployed")

    # 4. Failed OCI verification stops before OTel. A new image changes both Jaeger releases; only
    # default/jaeger is applied, alone although it needs default/prometheus.
    images("2" * 40)
    subprocess.run(ctl + ["-n", "default", "patch", "configmap", deploy.STATE, "--type=merge", "-p",
                          json.dumps({"data": dict.fromkeys(deploy.COMPONENTS, "stale")})], check=True, stdout=subprocess.DEVNULL)
    checks.fail = {"oci"}
    try:
        reconcile()
    except RuntimeError as error:
        assert "fixture oci health unavailable" in str(error)
    else:
        raise AssertionError("Failed OCI verification accepted")
    checks.fail = set()
    record = json.loads((root / helmfile_deploy.RECORD).read_text())
    assert record["outcome"] == "failed" and record["verifiedStacks"] == []
    assert checks.calls == [("prepare", ("oci", "otel")), ("verify", "oci")]
    otel = [c for c in commands if c[0] == "helmfile" and not c[c.index("-l") + 1].startswith("namespace=default,")]
    assert not otel, otel
    assert [line[2] + "/" + line[line.index("--namespace") + 1] for line in upgrades()] == ["jaeger/default"]
    after = revisions()
    assert after["default/jaeger"] == (2, "deployed") and after["jaeger/jaeger"] == (2, "deployed")
    assert fingerprints() == dict.fromkeys(deploy.COMPONENTS, "stale"), "A failed stack recorded fingerprints"
    record = reconcile()
    assert record["outcome"] == "succeeded" and record["verifiedStacks"] == ["oci", "otel"]
    assert [r["id"] for r in record["releases"] if r["changed"]] == ["jaeger/jaeger"]
    assert revisions()["jaeger/jaeger"] == (3, "deployed")

    # 5. An incompatible image fails at the gate, which leaves no receipt, and reconcile then
    # stops before its first cluster request.
    before = revisions()

    def incompatible(data, digest, renders, runtime):
        raise ValueError("Jaeger rejected the oci configuration")

    try:
        images("3" * 40, gate=incompatible)
    except ValueError as error:
        assert "rejected" in str(error)
    else:
        raise AssertionError("Incompatible image accepted")
    record = refused("No validated image selection")
    assert record["outcome"] == "refused" and [c[0] for c in commands] == ["git"], commands
    assert helm_log.read_text() == "" and revisions() == before

    # 6. Live mode, as in adoption: the images come from running Deployments, and the production
    # rollout, storage and Certificate checks run as the deployer. Only health and the trace are stubs.
    subprocess.run(ctl + ["apply", "-f", "-"], input=json.dumps(CERTIFICATE_CRD).encode(), check=True,
                   stdout=subprocess.DEVNULL)
    subprocess.run(ctl + ["wait", "--for", "condition=established", "--timeout=60s",
                          "crd/certificates.cert-manager.io"], check=True, stdout=subprocess.DEVNULL)
    expected = helmfile_deploy.expected_certificates(root)
    assert [ns for ns, _ in expected] == ["jaeger", "opensearch", "otel-demo"], expected
    for ns, name in expected:
        subprocess.run(ctl + ["apply", "-f", "-"], input=json.dumps(certificate(ns, name)).encode(), check=True,
                       stdout=subprocess.DEVNULL)
    subprocess.run(ctl + ["apply", "-f", "-"], input=json.dumps(workloads()).encode(), check=True, stdout=subprocess.DEVNULL)
    for item in workloads()["items"]:
        subprocess.run(ctl + ["-n", item["metadata"]["namespace"], "rollout", "status",
                              f"{item['kind'].lower()}/{item['metadata']['name']}", "--timeout=5m"], check=True,
                       stdout=subprocess.DEVNULL)

    def resolve_image(repo, tag, reference_digest=None):
        # The registry stand-in: the digest the node pulled for that reference.
        pods = json.loads(subprocess.check_output(ctl + ["get", "pods", "-A", "-o", "json"]))["items"]
        found = {status["imageID"].split("@")[-1] for pod in pods for status in pod["status"].get("containerStatuses", [])
                 if status["image"] in (f"docker.io/{repo}:{tag}", f"{repo}:{tag}")}
        digest, = found
        return {"repository": repo, "tag": tag, "sourceCommit": tag, "digest": digest, "platformManifest": digest}

    (root / "helmfile/images.yaml").write_text("mode: live\n")
    production = helmfile_deploy.ProductionChecks(root, run, health=lambda stack: {"stack": stack, "stub": True},
                                                  trace=lambda *versions: None, sleep=lambda s: None)
    commands.clear()
    record = helmfile_deploy.reconcile(dict(base), root=root, run=run, checks=production, verify_charts=charts,
                                       live_images=lambda r: helmfile_deploy.read_live_images(r, resolve_image=resolve_image))
    assert (record["outcome"], record["imageMode"], record["compatibility"], record["verifiedStacks"]) == \
        ("succeeded", "live", helmfile_deploy.NOT_REVALIDATED, ["oci", "otel"]), record
    for stack in ("oci", "otel"):
        for name, workload in (("jaeger", "jaeger"), ("hotrod", "jaeger-hotrod")):
            image = record["images"][stack][name]
            assert image["reference"] == f"docker.io/{LIVE_REPOS[workload]}:{LIVE_TAG}", image
            assert image["digest"].startswith("sha256:") and image["sourceCommit"] == LIVE_TAG, image
    assert "sha256" not in (root / helmfile_deploy.WORK / helmfile_deploy.LIVE_VALUES).read_text()
    assert not [c for c in commands if "restart" in c] and record["generatorRestarts"] == [], "Live mode restarted a generator"
    assert record["checks"]["otel"]["readyCertificates"] == [f"{ns}/{name}" for ns, name in expected]
    assert record["checks"]["otel"]["openSearchDataBindingUnchanged"] is True
    assert len(record["checks"]["oci"]["rollouts"]) == 4 and len(record["checks"]["otel"]["rollouts"]) == 8
    # The Certificate gate is strict: a missing or unready expected Certificate fails.
    ns, name = expected[0]
    subprocess.run(ctl + ["-n", ns, "apply", "-f", "-"], input=json.dumps(certificate(ns, name, ready="False")).encode(),
                   check=True, stdout=subprocess.DEVNULL)
    for state, message in (("unready", "is not Ready"), ("missing", "is missing")):
        if state == "missing":
            subprocess.run(ctl + ["-n", ns, "delete", "certificate", name], check=True, stdout=subprocess.DEVNULL)
        try:
            production.certificates()
        except helmfile_deploy.Failed as error:
            assert message in str(error), str(error)
        else:
            raise AssertionError(f"An {state} Certificate passed")

    print(json.dumps({"ranAsDeployer": True, "companionsInstalledByPlainHelmInstall": 4, "noOpNewRevisions": 0,
                      "noOpVerifiedStacks": ["oci", "otel"], "failedEmptyDiffSynced": True,
                      "atomicMapsToRollbackOnFailure": True, "pendingBlockedBeforeAnyWrite": True,
                      "ociFailureStoppedBeforeOTel": True, "selectorWithNeedsAppliedAlone": True,
                      "incompatibleImageStoppedBeforeClusterAccess": True, "fingerprintsPatchedOnlyWhenVerified": True,
                      "missingOrUnadoptedCompanionObjectRefused": True, "lookupSecretStableOnNoOp": True,
                      "liveImagesReadAsDeployer": True, "productionChecksOnKind": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", choices=(CONTEXT,), required=True)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Enable fixture assertions")
    exercise(args.kubeconfig, args.context)
