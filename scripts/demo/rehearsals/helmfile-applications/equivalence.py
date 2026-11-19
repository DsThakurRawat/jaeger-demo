#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Render the six hosted application releases both ways with public fixture inputs."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import yaml


ROOT = Path(__file__).resolve().parents[4]
STATE = ROOT / "helmfile.yaml"
CHART = ROOT / ".tools/helm-charts/charts/jaeger"
TARGET = ["--kube-version", "1.36.1", "--include-crds"]
OCI = ROOT / "examples/oci"
OTEL = ROOT / "examples/otel-demo"
# Exercise digit-only tags, independent pins, tag-only adoption and digest promotion.
IMAGES = {
    "oci": {
        "jaeger": {"repository": "jaegertracing/jaeger-snapshot", "tag": "0012345", "pullPolicy": "Always"},
        "hotrod": {"repository": "jaegertracing/example-hotrod-snapshot", "tag": "fedc123", "pullPolicy": "Always"},
    },
    "otel": {
        "jaeger": {"repository": "jaegertracing/jaeger-snapshot", "tag": "12ef000@sha256:" + "a" * 64,
                   "pullPolicy": "IfNotPresent"},
        "hotrod": {"repository": "jaegertracing/example-hotrod-snapshot", "tag": "feed000@sha256:" + "b" * 64,
                   "pullPolicy": "Always"},
    },
}


def run(*args):
    return subprocess.check_output(args, cwd=ROOT, env={**os.environ, "KUBECONFIG": os.devnull})


def legacy_version(directory, variable):
    source = (directory / "deploy-all.sh").read_text()
    # The original script exposes monitoring's pin as a shell default.
    assignment = re.search(rf'^{variable}="(?:\$\{{{variable}:-)?([\d.]+)\}}?"$', source, re.M)
    if not assignment:
        raise ValueError(f"Missing {variable} chart pin in {directory}")
    return assignment[1]


def legacy_jaeger(stack):
    if stack == "oci":
        directory, config = OCI, "config.yaml"
        overrides = ["provisionDataStore.cassandra=false", "storage.type=memory"]
    else:
        directory, config = OTEL, "jaeger-config.yaml"
        overrides = ["storage.type=elasticsearch", "storage.elasticsearch.anonymous=true",
                     "storage.elasticsearch.usePassword=false", "storage.elasticsearch.port=9200",
                     "storage.elasticsearch.host=opensearch-cluster-single.opensearch.svc.cluster.local"]
    overrides.append("allInOne.enabled=true")
    for field, name in (("allInOne", "jaeger"), ("hotrod", "hotrod")):
        for key, value in IMAGES[stack][name].items():
            overrides.append(f"{field}.image.{key}={value}")
    flags = ["-f", directory / "jaeger-values.yaml", "--set-file", f"userconfig={directory / config}",
             "--set-file", f"uiconfig={OCI / 'ui-config.json'}"]
    for override in overrides:
        flags.extend(["--set", override])
    return flags


class ManifestLoader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    # Prometheus CRD enums contain a literal "=". Kubernetes treats this as
    # text, while PyYAML otherwise gives it an unsupported YAML 1.1 tag.
    pass


ManifestLoader.add_constructor("tag:yaml.org,2002:value", ManifestLoader.construct_scalar)


def objects(render):
    result = {}
    for obj in yaml.load_all(render, Loader=ManifestLoader):
        if not obj:
            continue
        meta = obj["metadata"]
        key = (obj["apiVersion"], obj["kind"], meta.get("namespace", ""), meta["name"])
        if key in result:
            raise ValueError(f"Duplicate object {key}")
        result[key] = obj
    return result


def compare(namespace, name, chart, flags, fixture):
    # Grafana normally generates or looks up its admin password. Supply a public,
    # deterministic value to both offline template invocations.
    stable_input = ["--set", "grafana.adminPassword=synthetic-fixture-password"] if name == "prometheus" else []
    expected = objects(run("helm", "template", name, chart, "--namespace", namespace, *TARGET, *flags, *stable_input))
    actual = objects(run("helmfile", "-f", STATE, "--state-values-file", fixture, "--skip-deps",
                         "-l", f"namespace={namespace},name={name}", "template", "--skip-needs", *TARGET, *stable_input))
    differences = sorted(k for k in expected.keys() | actual.keys() if expected.get(k) != actual.get(k))
    if not expected or differences:
        raise ValueError(f"{namespace}/{name} render differs at {differences}")
    if name == "jaeger":
        stack = "oci" if namespace == "default" else "otel"
        for component, deploy in [("jaeger", "jaeger"), ("hotrod", "jaeger-hotrod")]:
            img = IMAGES[stack][component]
            containers = actual[("apps/v1", "Deployment", "", deploy)]["spec"]["template"]["spec"]["containers"]
            if not any(c["image"] == f"docker.io/{img['repository']}:{img['tag']}"
                       and c["imagePullPolicy"] == img["pullPolicy"] for c in containers):
                raise ValueError(f"Unexpected {stack}/{component} image or pull policy")
    print(f"{namespace}/{name}: {len(actual)} matching objects", flush=True)


def main():
    with tempfile.TemporaryDirectory() as temp:
        fixture = Path(temp) / "images.json"
        fixture.write_text(json.dumps({"images": IMAGES}))
        compare("default", "jaeger", CHART, legacy_jaeger("oci"), fixture)
        compare("jaeger", "jaeger", CHART, legacy_jaeger("otel"), fixture)
        charts = [
            ("default", "prometheus", OCI, "prometheus-community/kube-prometheus-stack",
             "PROMETHEUS_STACK_CHART_VERSION", "monitoring-values.yaml"),
            ("opensearch", "opensearch", OTEL, "opensearch/opensearch",
             "OPENSEARCH_CHART_VERSION", "opensearch-values.yaml"),
            ("opensearch", "opensearch-dashboards", OTEL, "opensearch/opensearch-dashboards",
             "OPENSEARCH_DASHBOARDS_CHART_VERSION", "opensearch-dashboard-values.yaml"),
            ("otel-demo", "otel-demo", OTEL, "open-telemetry/opentelemetry-demo",
             "OTEL_DEMO_CHART_VERSION", "otel-demo-values.yaml"),
        ]
        for namespace, name, directory, chart, pin, values in charts:
            flags = ["--version", legacy_version(directory, pin), "-f", directory / values]
            if name == "prometheus":
                flags.extend(["--set", "crds.upgradeJob.enabled=true", "--set", "crds.upgradeJob.forceConflicts=true"])
            compare(namespace, name, chart, flags, fixture)

        out = Path(temp) / "selected"
        run("helmfile", "-f", STATE, "--state-values-file", fixture, "--skip-deps",
            "-l", "stack=otel,component=otel-demo", "template", "--include-transitive-needs",
            "--output-dir", out, "--output-dir-template", "{{ .OutputDir }}/{{ .Release.Namespace }}/{{ .Release.Name }}",
            *TARGET)
        rendered = {path.relative_to(out).as_posix() for path in out.glob("*/*") if path.is_dir()}
        planned = set(run("python3", ROOT / "scripts/demo/helmfile_selection.py",
                          "--stack", "otel", "--component", "otel-demo").decode().splitlines())
        if rendered != planned:
            raise ValueError(f"Helmfile rendered {rendered}, selected {planned}")
        print("OpenTelemetry application and its transitive prerequisites matched the selection.")


if __name__ == "__main__":
    main()
