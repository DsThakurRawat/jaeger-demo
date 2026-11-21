#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Validate complete credential-free renders using the target Kubernetes and chart schemas."""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

import yaml

from helmfile_tools import CACHE, LOCK, cached_download, verify


class ManifestLoader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    """The Prometheus CRDs use a literal equals sign as an enum value."""


ManifestLoader.add_constructor("tag:yaml.org,2002:value", ManifestLoader.construct_scalar)


def schema_node(node, custom=False, structural=True, metadata_ref=None):
    """Convert OpenAPI schema nodes to draft-4, keeping arbitrary user data opaque."""
    if custom:
        supported = {"additionalProperties", "allOf", "anyOf", "default", "description", "enum", "example",
                     "exclusiveMaximum", "exclusiveMinimum", "format", "items", "maxItems", "maxLength",
                     "maxProperties", "maximum", "minItems", "minLength", "minProperties", "minimum",
                     "multipleOf", "not", "nullable", "oneOf", "pattern", "properties", "required", "title",
                     "type", "uniqueItems"}
        unknown = {k for k in node if k not in supported and not k.startswith("x-kubernetes-")}
        if unknown:
            raise ValueError(f"Unsupported structural CRD schema keywords: {sorted(unknown)}")
    out = {key: value for key, value in node.items() if key not in ("description", "nullable")}
    if node.get("format") == "int-or-string" or node.get("x-kubernetes-int-or-string"):
        out["type"] = ["integer", "string"]
        out.pop("format", None)
    if node.get("nullable") and "type" in out:
        kinds = out["type"] if isinstance(out["type"], list) else [out["type"]]
        out["type"] = [*kinds, "null"]
    embedded = custom and structural and node.get("x-kubernetes-embedded-resource")
    if "properties" in node or embedded:
        props = {}
        for name, prop in node.get("properties", {}).items():
            # Embedded metadata is governed by ObjectMeta and any declared validation rules.
            value = schema_node(prop, custom, structural and not (embedded and name == "metadata"), metadata_ref)
            # Kubernetes native typed decoding treats null on optional fields as omitted.
            if not custom and name not in node.get("required", []):
                value = {"anyOf": [value, {"type": "null"}]}
            props[name] = value
        if embedded:
            props.setdefault("apiVersion", {"type": "string"})
            props.setdefault("kind", {"type": "string"})
            meta = {"$ref": metadata_ref} if metadata_ref else {"type": "object"}
            props["metadata"] = {"allOf": [meta, props["metadata"]]} if "metadata" in props else meta
        out["properties"] = props
    if (structural and not node.get("x-kubernetes-preserve-unknown-fields") and
            ("properties" in node or custom and node.get("type") == "object")):
        out.setdefault("additionalProperties", False)
    for keyword in ("items", "additionalProperties", "not"):
        if isinstance(node.get(keyword), dict):
            out[keyword] = schema_node(node[keyword], custom, structural and keyword != "not", metadata_ref)
    for keyword in ("oneOf", "allOf", "anyOf"):
        if keyword in node:
            out[keyword] = [schema_node(item, custom, structural=False, metadata_ref=metadata_ref) for item in node[keyword]]
    return out


def gvk(api, kind):
    group, _, version = api.rpartition("/")
    if (group and not re.fullmatch(r"[a-z0-9][a-z0-9.-]*", group) or
            not re.fullmatch(r"[a-z0-9]+", version) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", kind)):
        raise ValueError(f"Invalid resource identity: {api!r}, {kind!r}")
    return group, version, kind


def prepare(openapi, manifests, folder):
    schemas = {}
    definitions = {name: schema_node(schema) for name, schema in openapi["definitions"].items()}
    shared = folder / "definitions.json"
    shared.write_text(json.dumps({"definitions": definitions}))
    for name, schema in definitions.items():
        for identity in schema.get("x-kubernetes-group-version-kind", []):
            key = identity["group"], identity["version"], identity["kind"]
            schemas[key] = {"$ref": f"{shared.as_uri()}#/definitions/{name}"}
    # Custom resources get ObjectMeta from Kubernetes and their structural schemas from the
    # exact rendered CRDs. No external CRD catalog or unversioned fallback is needed.
    requested = set()
    for manifest in manifests:
        with manifest.open() as source:
            for resource in yaml.load_all(source, Loader=ManifestLoader):
                if resource is None:
                    continue
                key = gvk(resource["apiVersion"], resource["kind"])
                requested.add(key)
                if key != ("apiextensions.k8s.io", "v1", "CustomResourceDefinition"):
                    continue
                spec = resource["spec"]
                for version in spec["versions"]:
                    if not version["served"]:
                        continue
                    identity = gvk(spec["group"] + "/" + version["name"], spec["names"]["kind"])
                    if identity in schemas:
                        raise ValueError(f"Duplicate served schema: {identity}")
                    meta_ref = f"{shared.as_uri()}#/definitions/io.k8s.apimachinery.pkg.apis.meta.v1.ObjectMeta"
                    root = version["schema"]["openAPIV3Schema"]
                    schema = schema_node(root, custom=True, metadata_ref=meta_ref)
                    # A structural CRD schema may omit these Kubernetes-managed envelope fields.
                    props = schema.setdefault("properties", {})
                    metadata = {"$ref": meta_ref}
                    declared = root.get("properties", {}).get("metadata")
                    if declared:
                        # Kubernetes enforces a CRD's own restrictions on metadata.name and generateName
                        # together with ObjectMeta. The fragment stays open so other metadata fields pass.
                        metadata = {"allOf": [metadata, schema_node(declared, custom=True, structural=False,
                                                                    metadata_ref=meta_ref)]}
                    props.update(apiVersion={"enum": [spec["group"] + "/" + version["name"]]},
                                 kind={"enum": [identity[2]]},
                                 metadata=metadata)
                    schemas[identity] = schema
    if not requested:
        raise ValueError("No resources in manifest input")
    for group, version, kind in sorted(requested):
        key = group, version, kind
        if key not in schemas:
            raise ValueError(f"Missing schema for {group or 'core'}/{version} {kind}. Include its served CRD in the render.")
        # kubeconform exposes a core resource's version under both Group and ResourceAPIVersion.
        path = folder / (group or version) / version / (kind.lower() + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(schemas[key]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifests", type=Path, nargs="+")
    args = parser.parse_args()
    pin = json.loads(LOCK.read_text())["kubernetesSchema"]
    version = pin["version"]
    url = f"https://raw.githubusercontent.com/kubernetes/kubernetes/{version}/api/openapi-spec/swagger.json"
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = cached_download(url, pin["sha256"], CACHE)
    with cached.open("rb") as stream:
        verify(stream, pin["sha256"], url)
        openapi = json.load(stream)
    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp)
        prepare(openapi, args.manifests, folder)
        return subprocess.run(["kubeconform", "-strict", "-summary", "-kubernetes-version", version[1:],
                               "-schema-location", str(folder / "{{.Group}}/{{.ResourceAPIVersion}}/{{.ResourceKind}}.json"),
                               *args.manifests]).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, yaml.YAMLError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
