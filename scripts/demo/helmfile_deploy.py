#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Deploy the hosted demo with Helmfile, the engine the deploy workflow runs when DEPLOY_ENGINE is helmfile.

  prepare    Build the Jaeger chart and the lock-verified .helmfile-locked.yaml. No cluster access.
  images     digest mode: resolve the snapshots, render both Jaeger releases without a cluster and run
             the Jaeger configuration gate. live mode: nothing to do.
  diff       Read-only: print which of the ten releases Helmfile would change.
  reconcile  Reconcile the ten releases OCI before OTel, verify both stacks and write deploy-record.json.

Every Helm and Helmfile call uses the pinned toolchain: the command re-runs itself through
helmfile_tools.py run unless that toolchain is already first on PATH.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

import yaml

import chart_archives
import companion_values
import deploy
import helmfile_selection
import live_observation
import promotion_images
import release_phases
import stack_health
import verify_trace


ROOT = Path(__file__).resolve().parents[2]
HELMFILE_TOOLS = ROOT / "scripts/demo/helmfile_tools.py"
TOOLS_LOCK = ROOT / "scripts/demo/helmfile-tools.lock.json"
JAEGER_CHART = ROOT / "scripts/demo/jaeger-chart.sh"
# Paths below are relative to the configuration root.
WORK = ".tools/helmfile-deploy"
SOURCE = "helmfile.yaml"
APPLICATIONS = ".helmfile-locked.yaml"
COMPANIONS = "helmfile-resources.yaml.gotmpl"
COMPANION_CHART = "charts/demo-resources"
IMAGE_CONFIG = "helmfile/images.yaml"
JAEGER_CHARTS = ".tools/helm-charts"
CHART_ARCHIVES = ".tools/chart-archives"
RECORD = "deploy-record.json"
SELECTION = "image-selection.json"
RECEIPT = "image-compatibility.json"
LIVE_VALUES = "live-images.json"
COMPANION_VALUES = "companions.json"

MODES = ("live", "digest")
OPERATIONS = ("apply", "install-companion")
EVENTS = ("schedule", "workflow_dispatch")
NOT_REVALIDATED = "live: not revalidated"
COMMIT = re.compile(r"[0-9a-f]{40}")
IMAGE_INPUTS = ("JAEGER_DEMO_IMAGE_TAG", "JAEGER_DEMO_JAEGER_IMAGE_TAG", "JAEGER_DEMO_HOTROD_IMAGE_TAG")
# Every release runs alone, upgrades in place and never installs or creates a namespace. Values
# come only from the state, so --reset-values drops anything an earlier writer set by hand. There
# is no global --rollback-on-failure: each release's own atomic setting applies.
FLAGS = ["--concurrency", "1", "--reset-values", "--sync-args=--install=false --create-namespace=false",
         "--diff-args=--allow-unreleased=false", "--suppress-secrets", "--suppress-diff"]
WORKLOADS = {"jaeger": "jaeger", "hotrod": "jaeger-hotrod"}
GENERATOR = {"oci": "default", "otel": "jaeger"}
OCI_ROLLOUTS = (("default", "deployment", "jaeger"), ("default", "deployment", "jaeger-hotrod"),
                ("default", "deployment", "trace-generator"), ("default", "deployment", "prometheus-grafana"))
OTEL_ROLLOUTS = (("opensearch", "statefulset", "opensearch-cluster-single"),
                 ("opensearch", "deployment", "opensearch-dashboards"), ("jaeger", "deployment", "jaeger"),
                 ("jaeger", "deployment", "jaeger-hotrod"), ("jaeger", "deployment", "trace-generator"),
                 ("otel-demo", "daemonset", "otel-collector-agent"), ("otel-demo", "deployment", "frontend"),
                 ("otel-demo", "deployment", "load-generator"))
CERTIFICATE_NAMESPACES = ("jaeger", "opensearch", "otel-demo")
HEALTH_ATTEMPTS, HEALTH_DELAY = 10, 30
REEXEC = "JAEGER_DEMO_HELMFILE_DEPLOY_PINNED"


class Refused(ValueError):
    """The run stops before any cluster access or change."""


def require(condition, message):
    if not condition:
        raise Refused(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def run_command(args, *, check=True, capture=False, env=None, cwd=None):
    """Run one command. Captured output is returned to the caller and never printed here."""
    pipe = subprocess.PIPE if capture else None
    return subprocess.run([str(arg) for arg in args], check=check, text=True, env=env, cwd=cwd,
                          stdout=pipe, stderr=pipe)


def workdir(root):
    path = Path(root) / WORK
    path.mkdir(parents=True, exist_ok=True)
    return path


def image_mode(root):
    config = yaml.safe_load((Path(root) / IMAGE_CONFIG).read_text())
    require(isinstance(config, dict) and set(config) == {"mode"} and config["mode"] in MODES,
            f"{IMAGE_CONFIG} must set mode to one of {', '.join(MODES)}")
    return config["mode"]


def offline_env():
    return dict(os.environ, KUBECONFIG=os.devnull)


def kube_version():
    return json.loads(TOOLS_LOCK.read_text())["kubernetesSchema"]["version"].removeprefix("v")


def refuse_live_inputs(env):
    require(env.get("GITHUB_EVENT_NAME") != "schedule",
            "A scheduled Helmfile run resolves its images: live mode is for manual adoption runs only")
    given = [name for name in IMAGE_INPUTS if env.get(name)]
    require(not given, "live mode reuses the live images; remove the image tag inputs " + ", ".join(given))


def release_rows(root):
    """All ten releases in execution order: OCI then OTel, applications in needs order, companions last."""
    releases = yaml.safe_load((Path(root) / SOURCE).read_text())["releases"]
    rows = []
    for stack in ("oci", "otel"):
        rows += [{"id": key, "kind": "application"} for key in helmfile_selection.selection(stack, "all", releases)]
        rows += [{"id": ns + "/" + release_phases.COMPANION, "kind": "companion"}
                 for ns in release_phases.NAMESPACES if release_phases.stack_of(ns + "/") == stack]
    return rows


def chart_versions(root):
    return {r["namespace"] + "/" + r["name"]: str(r.get("version", ""))
            for r in yaml.safe_load((Path(root) / SOURCE).read_text())["releases"]}


def verify_locked_charts(root):
    """Check, just before use, that the derived state is current and installs only locked archives."""
    root = Path(root)
    state = root / APPLICATIONS
    require(state.is_file(), f"{APPLICATIONS} is missing; run helmfile_deploy.py prepare first")
    text = state.read_text()
    require(f"# {SOURCE} sha256: {sha256((root / SOURCE).read_bytes())}\n" in text,
            f"{APPLICATIONS} is stale: {SOURCE} changed after prepare")
    require(f"# {chart_archives.LOCK} sha256: {sha256((root / chart_archives.LOCK).read_bytes())}\n" in text,
            f"{APPLICATIONS} is stale: {chart_archives.LOCK} changed after prepare")
    lock = chart_archives.load_lock(root)
    verified = {}
    for release in yaml.safe_load(text)["releases"]:
        chart = release["chart"]
        if chart.endswith(".tgz"):
            path = (root / chart).resolve()
            require(lock["charts"].get(path.name) == sha256(path.read_bytes()),
                    f"{path.name} differs from its reviewed SHA-256")
            verified[path.name] = lock["charts"][path.name]
        else:
            require(chart == "./" + JAEGER_CHARTS + "/charts/jaeger", f"Unexpected local chart {chart}")
    require(set(verified) == set(lock["charts"]), "The derived state does not use every locked chart archive")
    chart_archives.dependencies(root / JAEGER_CHARTS / "charts/jaeger/charts", root=root)
    commit = re.search(r'^JAEGER_CHART_COMMIT="([0-9a-f]{40})"$', JAEGER_CHART.read_text(), re.MULTILINE)[1]
    return {"charts": verified, "jaegerChartCommit": commit, "jaegerDependencies": lock["jaegerDependencies"]}


def tool_versions():
    tools = json.loads(TOOLS_LOCK.read_text())["tools"]
    try:
        import helmfile_tools
        platform = helmfile_tools.host()
    except ValueError:
        platform = None
    return {name: {"version": entry["version"],
                   "sha256": entry["sha256"].get(f"{entry['version']}/{platform}") if platform else None}
            for name, entry in tools.items()}


def git_head(root, run):
    head = run(["git", "-C", root, "rev-parse", "HEAD"], capture=True).stdout.strip()
    require(COMMIT.fullmatch(head), "Cannot read the configuration commit")
    return head


# ---------------------------------------------------------------- prepare

def prepare(root=ROOT, run=run_command):
    """Verify every chart the engine installs. Reaches public chart sources only, never a cluster."""
    root = Path(root)
    env = offline_env()
    charts = root / JAEGER_CHARTS
    charts.parent.mkdir(parents=True, exist_ok=True)
    run(["bash", "-c", 'source "$1" && prepare_jaeger_chart "$2"', "prepare", JAEGER_CHART, charts], env=env)
    chart_archives.dependencies(charts / "charts/jaeger/charts", root=root)
    archives = root / CHART_ARCHIVES
    archives.mkdir(parents=True, exist_ok=True)
    state = chart_archives.locked_state(root, archives)
    verified = verify_locked_charts(root)
    print(f"{state.relative_to(root)} installs only lock-verified archives: {', '.join(sorted(verified['charts']))}")
    return verified


# ---------------------------------------------------------------- images

def images(env, root=ROOT, run=run_command, resolve=None, validate=None):
    """digest mode: resolve, render offline and validate before any credential-holding step."""
    root = Path(root)
    work = workdir(root)
    for name in (SELECTION, RECEIPT):
        (work / name).unlink(missing_ok=True)
    mode = image_mode(root)
    if mode == "live":
        refuse_live_inputs(env)
        print(f"Image mode live: reconcile reads the live references; compatibility is recorded as '{NOT_REVALIDATED}'")
        return None
    event = env.get("GITHUB_EVENT_NAME", "")
    overrides = {"jaeger": env.get("JAEGER_DEMO_JAEGER_IMAGE_TAG") or None,
                 "hotrod": env.get("JAEGER_DEMO_HOTROD_IMAGE_TAG") or None}
    selection = (resolve or promotion_images.resolve)(event, overrides)
    data = (json.dumps(selection, indent=2, sort_keys=True) + "\n").encode()
    (work / SELECTION).write_bytes(data)
    renders = {}
    for stack in promotion_images.STACKS:
        renders[stack] = run(["helmfile", "-f", root / APPLICATIONS, "--state-values-file", work / SELECTION,
                              "--skip-deps", "-l", f"stack={stack},component=jaeger", "template", "--skip-needs",
                              "--kube-version", kube_version()],
                             capture=True, env=offline_env(), cwd=root).stdout.encode()
    receipt = (validate or promotion_images.validate)(data, sha256(data), renders, "docker")
    (work / RECEIPT).write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    for name, image in selection["sources"].items():
        print(f"{name}: {image['reference']} from {image['sourceCommit']} validated against both renders")
    return receipt


def digest_images(root, head):
    """The selection and receipt the images step wrote, checked against each other and this commit."""
    work = Path(root) / WORK
    require((work / SELECTION).is_file() and (work / RECEIPT).is_file(),
            "No validated image selection; the images step must pass before reconcile")
    data = (work / SELECTION).read_bytes()
    selection = json.loads(data)
    receipt = json.loads((work / RECEIPT).read_text())
    require(receipt.get("selectionSHA256") == sha256(data), "The compatibility receipt is for another selection")
    require(selection.get("configCommit") == head and receipt.get("configCommit") == head,
            "The image selection was made for another configuration commit")
    require(set(receipt.get("checks", {})) == set(promotion_images.STACKS)
            and all(c.get("result") == "passed" for c in receipt["checks"].values()),
            "The compatibility receipt does not pass both stacks")
    sources = selection["sources"]
    record = {stack: {name: {k: sources[name][k] for k in ("reference", "sourceCommit", "digest")}
                      for name in promotion_images.REPOS} for stack in promotion_images.STACKS}
    return work / SELECTION, record, receipt, selection.get("coreMain")


def live_image_values(observed, policies):
    """State values that render each live reference exactly, as a tag."""
    values, record = {}, {}
    for stack in promotion_images.STACKS:
        values[stack], record[stack] = {}, {}
        for name, repo in promotion_images.REPOS.items():
            image = observed[stack][name]
            match = re.fullmatch(r"docker\.io/" + re.escape(repo) + r":([\w][\w.-]{0,127})", image["reference"], re.ASCII)
            # The chart renders docker.io/<repository>:<tag>. Any other live form would change the pod template.
            require(match, f"The live {stack} {name} reference is not a tag in the form the Helmfile render produces")
            policy = policies[stack][name]
            require(policy in ("Always", "IfNotPresent", "Never"), f"Unexpected live {stack} {name} pull policy")
            values[stack][name] = {"repository": repo, "tag": match[1], "pullPolicy": policy}
            record[stack][name] = {k: image[k] for k in ("reference", "sourceCommit", "digest")}
    return {"images": values}, record


def read_live_images(run):
    """Read the live Jaeger and HotROD references of both stacks with GET requests only."""
    kubeconfig = os.environ.get("KUBECONFIG") or str(Path.home() / ".kube/config")
    require(os.pathsep not in kubeconfig, "Use a single kubeconfig file")
    context = run(["kubectl", "config", "current-context"], capture=True).stdout.strip()
    with live_observation.reader(kubeconfig, context) as get:
        observed = live_observation.snapshot(get, [], list(promotion_images.STACKS))["images"]
        policies = {}
        for stack, ns in promotion_images.STACKS.items():
            policies[stack] = {}
            for name, workload in WORKLOADS.items():
                spec = get(f"/apis/apps/v1/namespaces/{ns}/deployments/{workload}")["spec"]["template"]["spec"]
                container, = [c for c in spec["containers"] if c["name"] == workload]
                policies[stack][name] = container.get("imagePullPolicy")
    return observed, policies


def resolve_values(mode, root, head, run, live_images, env):
    """Return the application state values file, the image record and the compatibility entry."""
    work = workdir(root)
    if mode == "digest":
        return digest_images(root, head)
    observed, policies = (live_images or read_live_images)(run)
    values, record = live_image_values(observed, policies)
    path = work / LIVE_VALUES
    path.write_text(json.dumps(values, indent=2, sort_keys=True) + "\n")
    core = env.get("MAIN_SHA") if COMMIT.fullmatch(env.get("MAIN_SHA", "")) else None
    return path, record, NOT_REVALIDATED, core


# ---------------------------------------------------------------- Helm state

def helmfile_command(root, row, method, values, companions):
    ns, name = row["id"].split("/")
    if row["kind"] == "companion":
        state, state_values = Path(root) / COMPANIONS, companions
    else:
        state, state_values = Path(root) / APPLICATIONS, values
    return ["helmfile", "-f", state, "--state-values-file", state_values, "-l", f"namespace={ns},name={name}",
            "--skip-deps", method, *FLAGS]


def history(run, key):
    """The latest Helm revision of a release, or None only when Helm reports it not found."""
    ns, name = key.split("/")
    result = run(["helm", "history", name, "-n", ns, "--max", "1", "-o", "json"], check=False, capture=True)
    if result.returncode:
        if "release: not found" in (result.stderr or ""):
            return None
        raise RuntimeError(f"helm history {key} failed: {(result.stderr or '').strip()[-500:]}")
    entry, = json.loads(result.stdout)
    return {"revision": entry["revision"], "status": entry["status"], "chart": entry.get("chart"),
            "appVersion": entry.get("app_version")}


def listings(run):
    return {ns: json.loads(run(["helm", *release_phases.list_args(ns)], capture=True).stdout or "[]")
            for ns in release_phases.NAMESPACES}


# ---------------------------------------------------------------- verification

def retry(check, attempts=HEALTH_ATTEMPTS, delay=HEALTH_DELAY, sleep=time.sleep):
    for attempt in range(1, attempts + 1):
        try:
            return check()
        except (ValueError, OSError) as error:
            if attempt == attempts:
                raise
            print(f"Health check attempt {attempt}/{attempts} failed ({type(error).__name__}: {error}); retrying",
                  flush=True)
            sleep(delay)


class ProductionChecks:
    """The live checks of each stack. Each method raises on failure."""

    def __init__(self, root, run, health=stack_health.check, trace=verify_trace.verify,
                 binding=deploy.opensearch_data_binding, sleep=time.sleep):
        self.root, self.run, self.health, self.trace, self.binding, self.sleep = root, run, health, trace, binding, sleep
        self.before = None

    def prepare(self, stacks):
        # The OpenSearch data claim and its volume, read before the first write.
        if "otel" in stacks:
            self.before = self.binding()

    def rollouts(self, items):
        for ns, kind, name in items:
            self.run(["kubectl", "-n", ns, "rollout", "status", f"{kind}/{name}", "--timeout=5m"])
        return [f"{ns}/{kind}/{name}" for ns, kind, name in items]

    def certificates(self):
        found = 0
        for ns in CERTIFICATE_NAMESPACES:
            items = json.loads(self.run(["kubectl", "-n", ns, "get", "certificates.cert-manager.io", "-o", "json"],
                                        capture=True).stdout)["items"]
            for item in items:
                ready = [c for c in item.get("status", {}).get("conditions", []) if c.get("type") == "Ready"]
                if not ready or ready[0].get("status") != "True":
                    raise RuntimeError(f"Certificate {ns}/{item['metadata']['name']} is not Ready")
                found += 1
        if not found:
            raise RuntimeError("No Certificate found in the OTel namespaces")
        return found

    def verify(self, stack):
        if stack == "oci":
            rolled = self.rollouts(OCI_ROLLOUTS)
            return {"rollouts": rolled, "health": retry(lambda: self.health("oci"), sleep=self.sleep)}
        rolled = self.rollouts(OTEL_ROLLOUTS)
        if self.before is None or self.binding() != self.before:
            raise RuntimeError("The OpenSearch data claim or its bound volume changed during the run")
        versions = chart_versions(self.root)
        self.trace(versions["opensearch/opensearch"], versions["opensearch/opensearch-dashboards"])
        certificates = self.certificates()
        return {"rollouts": rolled, "openSearchDataBindingUnchanged": True, "freshTrace": "passed",
                "readyCertificates": certificates, "health": retry(lambda: self.health("otel"), sleep=self.sleep)}


# ---------------------------------------------------------------- guards and reconcile

def guards(env, root, run):
    event = env.get("GITHUB_EVENT_NAME", "")
    require(event in EVENTS, "The Helmfile engine runs only on a schedule or a manual dispatch")
    require(env.get("GITHUB_REF") == "refs/heads/main", "The Helmfile engine deploys main only")
    require(env.get("DEPLOY_ENGINE", "helmfile") == "helmfile", "DEPLOY_ENGINE does not select the Helmfile engine")
    head = git_head(root, run)
    require(env.get("GITHUB_SHA") == head, "The checkout differs from the workflow revision")
    if event == "workflow_dispatch":
        expected = env.get("EXPECTED_CONFIG_SHA", "")
        require(COMMIT.fullmatch(expected) and expected == head,
                "A manual Helmfile run requires expected_config_sha equal to the workflow revision")
    operation = env.get("HELMFILE_OPERATION") or "apply"
    namespace = env.get("COMPANION_NAMESPACE") or "none"
    require(operation in OPERATIONS, f"Unknown Helmfile operation {operation!r}")
    if operation == "apply":
        require(namespace == "none", "companion_namespace applies only to install-companion")
    else:
        require(event == "workflow_dispatch", "Only a manual dispatch installs a companion release")
        require(namespace in release_phases.NAMESPACES, "install-companion needs one companion_namespace")
    mode = image_mode(root)
    if mode == "live":
        refuse_live_inputs(env)
    return event, operation, namespace, mode, head


def reconcile(env, *, root=ROOT, run=run_command, checks=None, live_images=None,
              verify_charts=verify_locked_charts, sleep=time.sleep):
    """Reconcile the selected releases and record the run. Returns the record; raises on failure."""
    root = Path(root)
    record_path = root / RECORD
    record_path.unlink(missing_ok=True)
    record = {"engine": "helmfile", "event": env.get("GITHUB_EVENT_NAME"), "run": env.get("GITHUB_RUN_ID"),
              "outcome": "refused", "releases": [], "verifiedStacks": [], "checks": {}, "fingerprints": []}
    wrote = []
    try:
        event, operation, namespace, mode, head = guards(env, root, run)
        record.update(operation=operation, companionNamespace=None if namespace == "none" else namespace,
                      imageMode=mode, configCommit=head, tools=tool_versions(), charts=verify_charts(root))
        rows = release_rows(root)
        install = frozenset()
        if operation == "install-companion":
            install = frozenset({namespace + "/" + release_phases.COMPANION})
            rows = [row for row in rows if row["id"] in install]
        work = workdir(root)
        companions = companion_values.source_values(root)
        (work / COMPANION_VALUES).write_text(json.dumps(companions, indent=2, sort_keys=True) + "\n")
        for ns, values in companions["companions"].items():
            (work / f"companion-{ns}.json").write_text(json.dumps(values, indent=2, sort_keys=True) + "\n")

        # Reads only from here until release_phases.run.
        values, image_record, compatibility, core = resolve_values(mode, root, head, run, live_images, env)
        record.update(coreMain=core, images=image_record, compatibility=compatibility)
        if operation == "apply":
            state = run(["kubectl", "-n", "default", "get", "configmap", deploy.STATE, "--ignore-not-found",
                         "-o", "json"], capture=True).stdout
            require(state.strip(), f"The Helmfile engine requires the existing {deploy.STATE} ConfigMap")
        found = listings(run)
        before = {row["id"]: history(run, row["id"]) for row in rows}
        statuses = release_phases.selected_statuses(
            rows, found, lambda key: before[key]["status"] if before[key] else None)
        entries = {}
        for row in rows:
            prior = before[row["id"]] or {}
            entries[row["id"]] = {"id": row["id"], "statusBefore": statuses[row["id"]],
                                  "revisionBefore": prior.get("revision"), "chartBefore": prior.get("chart"),
                                  "method": None, "revisionAfter": None, "statusAfter": None, "changed": False}
            record["releases"].append(entries[row["id"]])
        checks = checks or ProductionChecks(root, run, sleep=sleep)
        checks.prepare({release_phases.stack_of(row["id"]) for row in rows} | {"oci"})

        def reconcile_one(row, method):
            entry = entries[row["id"]]
            entry["method"] = method
            wrote.append(row["id"])
            print(f"::group::{method} {row['id']}", flush=True)
            try:
                if method == "install":
                    ns = row["id"].split("/")[0]
                    run(["helm", *release_phases.install_args(ns, root / COMPANION_CHART, work / f"companion-{ns}.json")],
                        cwd=root)
                else:
                    run(helmfile_command(root, row, method, values, work / COMPANION_VALUES), cwd=root)
            finally:
                after = history(run, row["id"]) or {}
                entry.update(revisionAfter=after.get("revision"), statusAfter=after.get("status"),
                             changed=after.get("revision") != entry["revisionBefore"])
                print("::endgroup::", flush=True)

        def verify_stack(stack):
            print(f"::group::Verify {stack}", flush=True)
            try:
                record["checks"][stack] = checks.verify(stack)
                # Parity with the scripts until #2: restart the generator after a verified stack.
                ns = GENERATOR[stack]
                run(["kubectl", "-n", ns, "rollout", "restart", "deployment/trace-generator"])
                run(["kubectl", "-n", ns, "rollout", "status", "deployment/trace-generator", "--timeout=5m"])
                if operation == "apply":
                    # Keep the script engine's records current, one key at a time as deploy.py writes them.
                    for name, (target, _, _) in deploy.COMPONENTS.items():
                        if target == stack:
                            patch = json.dumps({"data": {name: deploy.fingerprint(name, root)}})
                            run(["kubectl", "-n", "default", "patch", "configmap", deploy.STATE, "--type=merge", "-p", patch])
                            record["fingerprints"].append(name)
                return True
            finally:
                print("::endgroup::", flush=True)

        receipt = release_phases.run(rows, statuses, reconcile_one, verify_stack, install=install)
        record["verifiedStacks"] = receipt["verifiedStacks"]
        record["outcome"] = "succeeded"
        return record
    except BaseException as error:
        record["outcome"] = "failed" if wrote else "refused"
        record["error"] = f"{type(error).__name__}: {error}"
        raise
    finally:
        record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        if env.get("GITHUB_STEP_SUMMARY"):
            with open(env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
                output.write(summary(record))


def summary(record):
    lines = ["## Helmfile deployment", "",
             f"- Outcome: **{record['outcome']}**",
             f"- Event: {record.get('event')}; operation: {record.get('operation')}; image mode: {record.get('imageMode')}",
             f"- Configuration commit: {record.get('configCommit')}; upstream Jaeger main: {record.get('coreMain')}",
             f"- Verified stacks: {', '.join(record['verifiedStacks']) or 'none'}"]
    compatibility = record.get("compatibility")
    if isinstance(compatibility, dict):
        compatibility = f"passed for selection {compatibility.get('selectionSHA256')}"
    lines.append(f"- Compatibility: {compatibility}")
    if record.get("error"):
        lines.append(f"- Error: {record['error']}")
    if record["releases"]:
        lines += ["", "| Release | Status before | Revision before | Method | Revision after | Changed |",
                  "| --- | --- | --- | --- | --- | --- |"]
        lines += [f"| {r['id']} | {r['statusBefore']} | {r['revisionBefore']} | {r['method'] or 'not run'} | "
                  f"{r['revisionAfter']} | {'yes' if r['changed'] else 'no'} |" for r in record["releases"]]
    if record.get("images"):
        lines += ["", "| Stack | Image | Reference | Source commit | Digest |", "| --- | --- | --- | --- | --- |"]
        lines += [f"| {stack} | {name} | {image['reference']} | {image['sourceCommit']} | {image['digest']} |"
                  for stack, entries in record["images"].items() for name, image in entries.items()]
    return "\n".join(lines) + "\n\nThe full record is the deploy-record artifact of this run.\n"


# ---------------------------------------------------------------- diff

def diff(env, root=ROOT, run=run_command, live_images=None):
    """Read-only: report which releases Helmfile would change. Returns the per-release result."""
    root = Path(root)
    mode = image_mode(root)
    head = git_head(root, run)
    values = resolve_values(mode, root, head, run, live_images, env)[0]
    work = workdir(root)
    (work / COMPANION_VALUES).write_text(json.dumps(companion_values.source_values(root), indent=2, sort_keys=True))
    results = {}
    for row in release_rows(root):
        command = helmfile_command(root, row, "diff", values, work / COMPANION_VALUES)[:-len(FLAGS)]
        command += ["--concurrency", "1", "--reset-values", "--diff-args=--allow-unreleased=false",
                    "--suppress-secrets", "--detailed-exitcode", "--context", "3"]
        code = run(command, check=False, cwd=root).returncode
        results[row["id"]] = {0: "no changes", 2: "changes"}.get(code, "error")
    for key, result in results.items():
        print(f"{key}: {result}")
    return results


# ---------------------------------------------------------------- entry point

def pinned_tools():
    plugins = os.environ.get("HELM_PLUGINS")
    if not plugins:
        return False
    tools = Path(plugins).parent / "bin"
    return all(shutil.which(name) == str(tools / name) for name in ("helm", "helmfile"))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=("prepare", "images", "diff", "reconcile"))
    args = parser.parse_args(argv)
    if not pinned_tools():
        if os.environ.get(REEXEC):
            parser.exit(1, "error: the pinned Helm and Helmfile are not first on PATH\n")
        os.execve(sys.executable, [sys.executable, str(HELMFILE_TOOLS), "run", "--", sys.executable,
                                   str(Path(__file__).resolve()), *argv], dict(os.environ, **{REEXEC: "1"}))
    env = dict(os.environ)
    try:
        if args.command == "prepare":
            prepare()
        elif args.command == "images":
            images(env)
        elif args.command == "diff":
            return 1 if "error" in diff(env).values() else 0
        else:
            reconcile(env)
    except (Refused, ValueError, KeyError, OSError, RuntimeError, subprocess.CalledProcessError,
            yaml.YAMLError) as error:
        print(f"error: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
