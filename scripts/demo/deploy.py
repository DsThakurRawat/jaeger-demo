#!/usr/bin/env python3

# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Reconcile independently deployed demo components.

The workflows that change the cluster share one concurrency group, so one
reconciliation runs at a time.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import handover
import recovery_policy


ROOT = Path(__file__).resolve().parents[2]
STATE = "jaeger-demo-deployment-revisions"
COMPONENTS = {
    "opensearch": ("otel", "opensearch", [
        "opensearch-values.yaml", "opensearch-dashboard-values.yaml",
    ]),
    "otel-jaeger": ("otel", "jaeger", [
        "jaeger-values.yaml", "jaeger-config.yaml", "jaeger-query-service.yaml",
        "load-generator.yaml", "load-generator/requirements.txt", "generate_traces.py",
        "ingress/ingress-jaeger.yaml", "ingress/ingress-opensearch.yaml",
        "ingress/ingress-otel-demo.yaml",
    ]),
    "otel-demo": ("otel", "otel-demo", ["otel-demo-values.yaml"]),
    "oci-jaeger": ("oci", "jaeger", [
        "jaeger-values.yaml", "config.yaml", "ui-config.json", "ingress.yaml",
        "load-generator/load-generator.yaml", "load-generator/generate_traces.py",
        "load-generator/requirements.txt",
    ]),
    "oci-monitoring": ("oci", "monitoring", ["monitoring-values.yaml", "prometheus-svc.yaml"]),
}
CHART_PINS = {
    "opensearch": ("OPENSEARCH_CHART_VERSION", "OPENSEARCH_DASHBOARDS_CHART_VERSION"),
    "otel-demo": ("OTEL_DEMO_CHART_VERSION",),
    "oci-monitoring": ("PROMETHEUS_STACK_CHART_VERSION",),
    "otel-jaeger": ("HOTROD_IMAGE_TAG",),
}
PIN_LINE = re.compile(r'^([A-Z][A-Z0-9_]*_CHART_VERSION|HOTROD_IMAGE_TAG)="([^"\n]+)"$', re.MULTILINE)


def run(args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)


def fingerprint(component, root=ROOT):
    stack, scope, files = COMPONENTS[component]
    directory = root / "examples" / ("otel-demo" if stack == "otel" else "oci")
    script = (directory / "deploy-all.sh").read_text()
    pins = dict(PIN_LINE.findall(script))
    inputs = [PIN_LINE.sub(lambda m: m[1] + '="<component pin>"', script)]
    inputs += [name + "=" + pins[name] for name in CHART_PINS.get(component, ())]
    inputs += [name + "\n" + (directory / name).read_text() for name in files]
    if scope == "jaeger":
        inputs.append((root / "scripts/demo/jaeger-chart.sh").read_text())
    elif scope == "opensearch":
        inputs.append((root / "scripts/demo/recovery_policy.py").read_text())
    return hashlib.sha256("\0".join(inputs).encode()).hexdigest()


def selected_components(event, stack, scope, mode, previous, desired):
    if event == "workflow_dispatch":
        return [name for name, (target, part, _) in COMPONENTS.items()
                if stack in (target, "all") and (mode == "clean" or scope == "all" or scope == part)]
    return [name for name in COMPONENTS if previous.get(name) != desired[name]
            or (event == "schedule" and COMPONENTS[name][1] == "jaeger")]


def resolve_snapshots(env):
    # The resolver exports through GITHUB_ENV, which a child cannot apply to us.
    with tempfile.NamedTemporaryFile(mode="r+") as output:
        resolver_env = dict(env, GITHUB_ENV=output.name)
        if env.get("GITHUB_EVENT_NAME") == "push":
            resolver_env["GITHUB_EVENT_NAME"] = "schedule"
        run(["bash", str(ROOT / "scripts/utils/resolve-demo-snapshot-tags.sh")], env=resolver_env)
        for line in output:
            key, value = line.strip().split("=", 1)
            if key in ("JAEGER_DEMO_JAEGER_IMAGE_TAG", "JAEGER_DEMO_HOTROD_IMAGE_TAG", "JAEGER_DEMO_IMAGE_TAG"):
                env[key] = value


class ScheduledReleaseError(RuntimeError):
    """A selected release cannot be upgraded in place."""


def check_scheduled_releases(component):
    # Check each selected release before attempting an in-place upgrade.
    releases = {
        "opensearch": ("opensearch", ("opensearch", "opensearch-dashboards")),
        "otel-jaeger": ("jaeger", ("jaeger",)),
        "otel-demo": ("otel-demo", ("otel-demo",)),
        "oci-jaeger": ("default", ("jaeger",)),
        "oci-monitoring": ("default", ("prometheus",)),
    }
    namespace, names = releases[component]
    for name in names:
        result = run(["helm", "status", name, "--namespace", namespace, "-o", "json"], capture_output=True)
        release = json.loads(result.stdout)
        status = release["info"]["status"]
        # Non-atomic chart upgrades may leave a failed release to repair in
        # place. Neither Jaeger path may reinstall or replace a failed release.
        if status != "deployed" and not (status == "failed" and name != "jaeger"):
            raise ScheduledReleaseError(f"Cannot schedule {namespace}/{name} with Helm status {status}")


def opensearch_data_binding(get=None):
    def read_object(kind, name):
        return json.loads(run(["kubectl", "-n", "opensearch", "get", kind, name, "-o", "json"],
                              capture_output=True).stdout)

    get = get or read_object
    pod = get("pod", "opensearch-cluster-single-0")
    spec = pod.get("spec", {})
    mounts = {mount["name"] for container in spec.get("containers", []) if container["name"] == "opensearch"
              for mount in container.get("volumeMounts", []) if mount["mountPath"] == "/usr/share/opensearch/data"}
    claims = [v.get("persistentVolumeClaim", {}).get("claimName") for v in spec.get("volumes", [])
              if v["name"] in mounts]
    if len(claims) != 1 or not claims[0]:
        raise ScheduledReleaseError("Cannot identify the existing OpenSearch data PVC")
    claim = get("pvc", claims[0])
    metadata = claim.get("metadata", {})
    uid, pv = metadata.get("uid"), claim.get("spec", {}).get("volumeName")
    if not uid or not pv or claim.get("status", {}).get("phase") != "Bound" or metadata.get("deletionTimestamp"):
        raise ScheduledReleaseError("Scheduled OpenSearch updates require a bound, active data PVC")
    return claims[0], uid, pv


def deploy_component(component, env):
    stack, scope, _ = COMPONENTS[component]
    directory = ROOT / "examples" / ("otel-demo" if stack == "otel" else "oci")
    child_env = dict(env, JAEGER_DEMO_STACK=stack, JAEGER_DEMO_DEPLOY_SCOPE=scope,
                     JAEGER_OTEL_DEMO_DEPLOY_SCOPE=scope)
    mode = env.get("JAEGER_DEMO_DEPLOY_MODE", "upgrade")
    if env.get("GITHUB_EVENT_NAME") == "schedule":
        check_scheduled_releases(component)
    binding = None
    if scope == "opensearch" and mode == "upgrade":
        policy = recovery_policy.resolve(env, stack, mode, scope, env.get("JAEGER_OTEL_DEMO_OPENSEARCH_RECOVERY", ""))
        if policy == "preserve-pvc":
            binding = opensearch_data_binding()
        elif policy == "required":
            if env.get("GITHUB_OUTPUT"):
                with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                    output.write("recovery_started=true\n")
            run(["bash", str(directory / "opensearch-recovery.sh"), "backup-test"], env=child_env)
    run(["bash", str(directory / "deploy-all.sh"), mode], env=child_env)
    if scope == "opensearch":
        if binding is not None and opensearch_data_binding() != binding:
            raise ScheduledReleaseError("OpenSearch data PVC or bound PV changed during the scheduled upgrade")
        pins = dict(PIN_LINE.findall((directory / "deploy-all.sh").read_text()))
        run(["python3", str(ROOT / "scripts/demo/verify_trace.py"), pins["OPENSEARCH_CHART_VERSION"],
             pins["OPENSEARCH_DASHBOARDS_CHART_VERSION"]], env=child_env)


def reconcile(env, root=ROOT):
    event = env.get("GITHUB_EVENT_NAME", "workflow_dispatch")
    if event not in ("push", "schedule", "workflow_dispatch"):
        raise ValueError("Unsupported demo deployment event")
    if event != "workflow_dispatch" and env.get("GITHUB_REF") != "refs/heads/main":
        raise ValueError("Automatic demo deployment requires main")
    mode = env.get("JAEGER_DEMO_DEPLOY_MODE", "upgrade")
    stack = env.get("JAEGER_DEMO_STACK", "oci")
    scope = env.get("JAEGER_OTEL_DEMO_DEPLOY_SCOPE", "jaeger")
    reviewed_handover = handover.validate_inputs(env, stack, mode, scope)
    if stack not in ("oci", "otel", "all") or mode not in ("upgrade", "clean"):
        raise ValueError("Invalid demo stack or mode")
    if event != "workflow_dispatch" and mode != "upgrade":
        raise ValueError("Automatic demo deployments must use upgrade mode")
    recovery_policy.resolve(env, stack, mode, scope, env.get("JAEGER_OTEL_DEMO_OPENSEARCH_RECOVERY", ""))
    if event == "schedule":
        handover.check_helm_context(env)
    if event == "workflow_dispatch" and mode == "clean":
        # Clean is an explicit whole-stack operation, not a per-component loop.
        resolve_snapshots(env)
        for target in ("oci", "otel"):
            if stack in (target, "all"):
                child = dict(env, JAEGER_DEMO_STACK=target, JAEGER_DEMO_DEPLOY_SCOPE="all")
                directory = root / "examples" / ("otel-demo" if target == "otel" else "oci")
                run(["bash", str(directory / "deploy-all.sh"), "clean"], env=child)
        return 0

    result = run(["kubectl", "-n", "default", "get", "configmap", STATE,
                  "--ignore-not-found", "-o", "json"], capture_output=True)
    if event == "schedule" and not result.stdout.strip():
        raise ValueError("Scheduled upgrades require the existing deployment revision ConfigMap")
    state_exists = bool(result.stdout.strip())
    previous = json.loads(result.stdout).get("data", {}) if state_exists else {}
    desired = {name: fingerprint(name, root) for name in COMPONENTS}
    selected = selected_components(event, stack, scope, mode, previous, desired)
    if event == "workflow_dispatch" and not selected:
        raise ValueError("Invalid scope for selected demo stack")
    if reviewed_handover:
        if selected != [stack + "-jaeger"]:
            raise ValueError("Handover selected components differ from the reviewed single Jaeger release")
        handover.preflight(env, stack, mode, scope)
    failures = []
    snapshots_resolved = False
    for name in selected:
        if name.startswith("otel-") and "opensearch" in failures:
            print(f"::error::{name}: blocked by failed OpenSearch deployment", flush=True)
            failures.append(name)
            continue
        try:
            if COMPONENTS[name][1] == "jaeger" and not snapshots_resolved:
                image_inputs = {key: env.get(key) for key in (
                    "JAEGER_DEMO_JAEGER_IMAGE_TAG", "JAEGER_DEMO_HOTROD_IMAGE_TAG")}
                resolve_snapshots(env)
                if reviewed_handover:
                    handover.validate_inputs(env, stack, mode, scope)
                    if any(env.get(key) != value for key, value in image_inputs.items()):
                        raise ValueError("Handover image resolution changed the reviewed commit tags")
                snapshots_resolved = True
            print(f"::group::Deploy {name}", flush=True)
            deploy_component(name, env)
            # Write one key at a time, so a concurrent or earlier writer's other
            # keys and the object's annotations are never replaced.
            if state_exists:
                patch = json.dumps({"data": {name: desired[name]}})
                run(["kubectl", "-n", "default", "patch", "configmap", STATE, "--type=merge", "-p", patch])
            else:
                # Only a first manual install reaches this; a scheduled run requires the record above.
                run(["kubectl", "-n", "default", "create", "configmap", STATE,
                     "--from-literal=" + name + "=" + desired[name]])
                state_exists = True
            print(f"{name}: deployed successfully", flush=True)
        except (subprocess.CalledProcessError, ScheduledReleaseError) as error:
            print(f"::error::{name}: {error}", flush=True)
            failures.append(name)
        finally:
            print("::endgroup::", flush=True)
    summary = "\n".join(f"- {name}: {'FAILED' if name in failures else 'deployed'}" for name in selected)
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write("## Demo deployments\n\n" + (summary or "No component changes.") + "\n")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(reconcile(dict(os.environ)))
