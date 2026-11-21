#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Render inputs for the supplemental chart from the original deploy script sources."""

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
MANIFESTS = {
    "default": ("oci/ingress.yaml", "oci/prometheus-svc.yaml", "oci/load-generator/load-generator.yaml"),
    "jaeger": ("otel-demo/jaeger-query-service.yaml", "otel-demo/load-generator.yaml", "otel-demo/ingress/ingress-jaeger.yaml"),
    "opensearch": ("otel-demo/ingress/ingress-opensearch.yaml",),
    "otel-demo": ("otel-demo/ingress/ingress-otel-demo.yaml",),
}
GENERATORS = {
    "default": ("oci/load-generator/generate_traces.py", "oci/load-generator/requirements.txt"),
    "jaeger": ("otel-demo/generate_traces.py", "otel-demo/load-generator/requirements.txt"),
}


def source_values(root=ROOT):
    examples = root / "examples"
    companions = {}
    for namespace, paths in MANIFESTS.items():
        objects = []
        for path in paths:
            objects.extend(yaml.safe_load_all((examples / path).read_text()))
        if namespace in GENERATORS:
            # kubectl create configmap --from-file uses the basename and the full file.
            data = {Path(path).name: (examples / path).read_bytes().decode("utf-8") for path in GENERATORS[namespace]}
            objects.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "trace-script"}, "data": data})
        companions[namespace] = {"objects": objects}
    return {"companions": companions}


if __name__ == "__main__":
    print(json.dumps(source_values(), indent=2))
