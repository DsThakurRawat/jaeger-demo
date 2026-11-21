#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Render the six hosted application releases both ways with public fixture inputs."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import yaml


ROOT = Path(__file__).resolve().parents[4]
STATE = ROOT / "helmfile.yaml"
TARGET = ["--kube-version", "1.37.0", "--include-crds"]
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


def legacy_jaeger(stack, root=ROOT):
    oci = root / "examples/oci"
    otel = root / "examples/otel-demo"
    if stack == "oci":
        directory, config = oci, "config.yaml"
        overrides = ["provisionDataStore.cassandra=false", "storage.type=memory"]
    else:
        directory, config = otel, "jaeger-config.yaml"
        overrides = ["storage.type=elasticsearch", "storage.elasticsearch.anonymous=true",
                     "storage.elasticsearch.usePassword=false", "storage.elasticsearch.port=9200",
                     "storage.elasticsearch.host=opensearch-cluster-single.opensearch.svc.cluster.local"]
    overrides.append("allInOne.enabled=true")
    for field, name in (("allInOne", "jaeger"), ("hotrod", "hotrod")):
        for key, value in IMAGES[stack][name].items():
            overrides.append(f"{field}.image.{key}={value}")
    flags = ["-f", directory / "jaeger-values.yaml", "--set-file", f"userconfig={directory / config}",
             "--set-file", f"uiconfig={oci / 'ui-config.json'}"]
    for override in overrides:
        flags.extend(["--set", override])
    return flags


def legacy_inputs(root):
    oci, otel = root / "examples/oci", root / "examples/otel-demo"
    chart = root / ".tools/helm-charts/charts/jaeger"
    for namespace, stack in (("default", "oci"), ("jaeger", "otel")):
        yield namespace, "jaeger", chart, legacy_jaeger(stack, root)
    charts = [
        ("default", "prometheus", oci, "prometheus-community/kube-prometheus-stack",
         "PROMETHEUS_STACK_CHART_VERSION", "monitoring-values.yaml"),
        ("opensearch", "opensearch", otel, "opensearch/opensearch",
         "OPENSEARCH_CHART_VERSION", "opensearch-values.yaml"),
        ("opensearch", "opensearch-dashboards", otel, "opensearch/opensearch-dashboards",
         "OPENSEARCH_DASHBOARDS_CHART_VERSION", "opensearch-dashboard-values.yaml"),
        ("otel-demo", "otel-demo", otel, "open-telemetry/opentelemetry-demo",
         "OTEL_DEMO_CHART_VERSION", "otel-demo-values.yaml"),
    ]
    for namespace, name, directory, chart, pin, values in charts:
        flags = ["--version", legacy_version(directory, pin), "-f", directory / values]
        if name == "prometheus":
            flags.extend(["--set", "crds.upgradeJob.enabled=true", "--set", "crds.upgradeJob.forceConflicts=true"])
        yield namespace, name, chart, flags


def stable_values(name):
    return ["--set", "grafana.adminPassword=synthetic-fixture-password"] if name == "prometheus" else []


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
    stable_input = stable_values(name)
    expected = objects(run("helm", "template", name, chart, "--namespace", namespace, *TARGET, *flags, *stable_input))
    payload = run("helmfile", "-f", STATE, "--state-values-file", fixture, "--skip-deps",
                         "-l", f"namespace={namespace},name={name}", "template", "--skip-needs", *TARGET, *stable_input)
    actual = objects(payload)
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
    output = fixture.parent / f"{namespace}-{name}.yaml"
    output.write_bytes(payload)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate", action="store_true", help="Schema-check application and companion renders")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temp:
        fixture = Path(temp) / "images.json"
        fixture.write_text(json.dumps({"images": IMAGES}))
        manifests = [compare(namespace, name, chart, flags, fixture)
                     for namespace, name, chart, flags in legacy_inputs(ROOT)]

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

        if args.validate:
            companion_values = Path(temp) / "companions.json"
            companion_values.write_bytes(run("python3", ROOT / "scripts/demo/companion_values.py"))
            companions = Path(temp) / "companions.yaml"
            companions.write_bytes(run("helmfile", "-f", ROOT / "helmfile-resources.yaml.gotmpl",
                                       "--state-values-file", companion_values, "template", "--skip-deps"))
            subprocess.run(["python3", ROOT / "scripts/demo/validate_helmfile.py", *manifests, companions], check=True)



if __name__ == "__main__":
    main()
