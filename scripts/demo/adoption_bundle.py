#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Seal, verify and consume a frozen private Helmfile plan without network resolution."""

import argparse
import base64
import binascii
import bz2
import contextlib
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
from types import MappingProxyType
import re
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import zipfile

from cryptography.fernet import Fernet
import yaml

import release_phases


FORMAT = "jaeger-demo-helmfile-plan-v1"
# A complete six-release render is about 6 MiB. Bound memory before reading or decrypting.
MAX_PLAN = 64 * 1024 * 1024
MAX_TOKEN = 4 * ((MAX_PLAN + 256) // 3 + 1)
MAX_MEMBER = 32 * 1024 * 1024
RELEASES = {
    "default/jaeger", "default/prometheus", "jaeger/jaeger", "opensearch/opensearch",
    "opensearch/opensearch-dashboards", "otel-demo/otel-demo",
    *(f"{ns}/demo-resources" for ns in ("default", "jaeger", "opensearch", "otel-demo")),
}
COMMON = {"plan.json", "tools.lock.json", "live.json"}
# Reviewed SHA-256 of each repository chart archive, shared with the daily executor.
CHART_ARCHIVES = "ops/executor/chart-archives.json"
HOOK_PREFIX = "helm.sh/hook"
SHA = re.compile(r"[a-f0-9]{64}")
COMMIT = re.compile(r"[a-f0-9]{40}")
IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/+:@-]*")
FIELD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*")


class BundleError(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise BundleError(message)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def read_json(data):
    def pairs(items):
        obj = dict(items)
        require(len(obj) == len(items), "Duplicate JSON property")
        return obj
    def invalid_number(_):
        raise BundleError("Non-finite JSON number")
    return json.loads(data, object_pairs_hook=pairs, parse_constant=invalid_number)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value)


def private_directory(path):
    info = path.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077,
            "Use an existing owner-only directory (mode 0700)")


def read_member(folder, name, limit=MAX_MEMBER, private=False):
    # Open each path component relative to its parent fd, refusing links, devices and FIFOs.
    with contextlib.ExitStack() as opened:
        flags = os.O_RDONLY | os.O_NOFOLLOW
        handle = os.open(folder, flags | os.O_DIRECTORY)
        opened.callback(os.close, handle)
        parts = PurePosixPath(name).parts
        require(parts and not name.startswith("/") and ".." not in parts, "Invalid relative member path")
        for part in parts[:-1]:
            handle = os.open(part, flags | os.O_DIRECTORY, dir_fd=handle)
            opened.callback(os.close, handle)
        stream = opened.enter_context(os.fdopen(os.open(parts[-1], flags | os.O_NONBLOCK, dir_fd=handle), "rb"))
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_size <= limit, "Expected a bounded regular file")
        if private:
            require(info.st_uid == os.getuid() and not info.st_mode & 0o077, "Key file must be owner-only")
        data = stream.read(limit + 1)
        require(len(data) <= limit, "Member exceeds size limit")
        return data


def read_key(path):
    return Fernet(read_member(path.parent, path.name, 65, private=True).strip())


def verify_source(checkout, expected):
    require(matches(COMMIT, expected), "Expected full reviewed configuration commit")
    def git(*args):
        return subprocess.check_output(["git", "-C", str(checkout), *args], text=True).strip()
    require(git("rev-parse", "HEAD") == expected, "Configuration checkout differs from reviewed commit")
    require(not git("status", "--porcelain=v1", "--untracked-files=normal"), "Configuration checkout has local changes")


def verify_frozen_source(checkout, expected, files, plan):
    def blob(name, stderr=None):
        return subprocess.check_output(["git", "-C", str(checkout), "show", f"{expected}:{name}"], stderr=stderr)
    require(files["tools.lock.json"] == blob("scripts/demo/helmfile-tools.lock.json"),
            "Bundled tools differ from the reviewed source tree")
    selector = plan["selector"]
    stack, component = selector["stack"], selector["component"]
    require(stack in ("oci", "otel", "all"), "Invalid stack selector")
    if component == "demo-resources":
        allowlist = yaml.safe_load(blob("charts/demo-resources/allowlist.yaml"))
        direct = {f"{ns}/demo-resources" for ns in allowlist
                  if stack == "all" or stack == ("oci" if ns == "default" else "otel")}
        require(plan["mode"] == "adoption" and {r["id"] for r in plan["releases"]} == direct,
                "Companion ownership is a separate adoption stage")
        metadata = yaml.safe_load(blob("charts/demo-resources/Chart.yaml"))
        for row in plan["releases"]:
            require(not row["needs"] and row["chart"]["source"] == "charts/demo-resources" and
                    row["chart"]["name"] == metadata["name"] and row["chart"]["version"] == str(metadata["version"]),
                    "Companion source mismatch")
            # Helm rewrites Chart.yaml while packaging. All chart templates and input files
            # must be byte-identical to their committed sources, with no extra hooks.
            source_names = subprocess.check_output(["git", "-C", str(checkout), "ls-tree", "-r", "--name-only",
                                expected, "--", "charts/demo-resources"], text=True).splitlines()
            chart_files = {n.removeprefix("charts/demo-resources/"): n for n in source_names}
            packed = files[f"releases/{row['id']}/chart.tgz"]
            with tarfile.open(fileobj=io.BytesIO(packed), mode="r:gz") as tar:
                members = {m.name.removeprefix(metadata["name"]+"/"):m for m in tar if m.isfile()}
                require(members.keys() == chart_files.keys(), "Unexpected companion chart files")
                for name, path in chart_files.items():
                    contents = tar.extractfile(members[name]).read()
                    if name == "Chart.yaml":
                        require(yaml.safe_load(contents) == metadata, "Companion chart metadata drift")
                    else:
                        require(contents == blob(path), "Companion chart file differs from reviewed source")
            ns = row["id"].split("/")[0]
            allowed = {(kind, name) for kind, names in allowlist[ns].items() for name in names}
            seen = set()
            data = files[f"releases/{row['id']}/rendered.yaml"]
            for obj in yaml.load_all(data, Loader=ManifestLoader):
                if obj is None:
                    continue
                meta = obj["metadata"]
                identity = obj["kind"], meta["name"]
                annotations = meta.get("annotations") or {}
                # A YAML tag such as !!binary loads as a non-text key that Helm still reads as text.
                require(isinstance(annotations, dict) and all(isinstance(name, str) for name in annotations),
                        "Companion resource annotations must be a mapping with text keys")
                require(meta.get("namespace", ns) == ns and identity not in seen and
                        annotations.get("helm.sh/resource-policy") == "keep",
                        "Unsafe or duplicate companion resource")
                # Helm does not own a hook, ignores its keep policy and by default deletes
                # and recreates it before each run.
                require(not any(name.startswith(HOOK_PREFIX) for name in annotations),
                        "Companion resource must not be a Helm hook")
                seen.add(identity)
            require(seen == allowed, "Companion render differs from the checked-in allowlist")
    else:
        state = yaml.safe_load(blob("helmfile.yaml"))["releases"]
        by_id = {r["namespace"] + "/" + r["name"]: r for r in state}
        direct = {k for k, r in by_id.items() if stack in ("all", r["labels"]["stack"]) and
                  component in ("all", r["labels"]["component"])}
        reviewed = None
        for row in plan["releases"]:
            require(row["id"] in by_id, "Application and companion stages must be separate")
            release = by_id[row["id"]]
            require(row["needs"] == release.get("needs", []), "Prerequisites differ from the checked-in Helmfile")
            if "version" in release:
                # The plan and the archive both come from the bundle. Bind the chart identity
                # and its exact bytes to the reviewed tree, as the daily executor does.
                name, version = release["chart"].rpartition("/")[2], str(release["version"])
                require(row["chart"]["source"] == release["chart"] and row["chart"]["name"] == name and
                        row["chart"]["version"] == version, "Chart differs from checked-in Helmfile")
                if reviewed is None:
                    try:
                        document = read_json(blob(CHART_ARCHIVES, subprocess.DEVNULL))
                    except subprocess.CalledProcessError:
                        raise BundleError("Reviewed chart checksums are missing from the source tree") from None
                    except ValueError:
                        raise BundleError("Invalid reviewed chart checksums") from None
                    reviewed = document.get("charts") if isinstance(document, dict) else None
                    require(isinstance(reviewed, dict), "Invalid reviewed chart checksums")
                expected_sha = reviewed.get(f"{name}-{version}.tgz")
                require(matches(SHA, expected_sha) and row["chart"]["sha256"] == expected_sha,
                        "Chart archive differs from the reviewed SHA-256")
            elif release["name"] == "jaeger":
                # This chart is packaged locally from the pinned commit, so no reviewed archive
                # checksum exists. Its bytes are bound by the approved bundle digest alone.
                pin = re.search(rb'^JAEGER_CHART_COMMIT="([a-f0-9]{40})"$',
                                blob("scripts/demo/jaeger-chart.sh"), re.MULTILINE)
                require(pin and row["chart"]["name"] == "jaeger" and
                        row["chart"]["source"] == "https://github.com/jaegertracing/helm-charts.git@" +
                        pin[1].decode(), "Jaeger chart source differs from the checked-in commit")
            else:
                raise BundleError("Application chart is missing a reviewed version")
    require(direct == set(plan["requested"]), "Requested releases differ from the checked-in selector")


def selection(plan, expected):
    require(plan["format"] == FORMAT and plan["configurationSHA"] == expected, "Plan source or format mismatch")
    require(plan["mode"] in ("adoption", "promotion"), "Unknown plan mode")
    caps = plan["renderContext"]
    require(re.fullmatch(r"v\d+\.\d+\.\d+", caps["kubernetesVersion"]) and
            isinstance(caps["apiVersions"], list) and caps["apiVersions"] and
            caps["apiVersions"] == sorted(set(caps["apiVersions"])) and
            all(matches(FIELD, api) for api in caps["apiVersions"]), "Incomplete Kubernetes rendering capabilities")
    requested = plan["requested"]
    require(isinstance(requested, list) and requested and len(set(requested)) == len(requested)
            and set(requested) <= RELEASES, "Invalid requested releases")
    rows = plan["releases"]
    require(isinstance(rows, list) and 0 < len(rows) <= len(RELEASES), "Invalid resolved release count")
    chosen = {}
    for row in rows:
        key = row["id"]
        require(key in RELEASES and key not in chosen, "Duplicate or unknown release")
        needs = row["needs"]
        require(isinstance(needs, list) and set(needs) <= chosen.keys(), "Missing or out-of-order prerequisite")
        require(not key.startswith("default/") or all(k.startswith("default/") for k in chosen),
                "OCI releases must precede OTel releases")
        chart = row["chart"]
        require(all(isinstance(chart.get(k), str) and chart[k] for k in ("name", "version", "source")) and
                matches(SHA, chart["sha256"]), "Incomplete chart provenance")
        helm = row["helm"]
        require(isinstance(helm, dict) and helm.keys() == {"operation", "atomic", "wait", "timeoutSeconds",
                "historyMax", "skipCRDs", "createNamespace"} and helm["operation"] in ("install", "upgrade")
                and type(helm["atomic"]) is bool and type(helm["skipCRDs"]) is bool and
                helm["createNamespace"] is False and helm["wait"] in ("watcher", "legacy")
                and type(helm["timeoutSeconds"]) is int and helm["timeoutSeconds"] > 0
                and type(helm["historyMax"]) is int and helm["historyMax"] > 0, "Incomplete or unsafe Helm options")
        require(not (helm["atomic"] and (key == "opensearch/opensearch" or
                key.endswith("/demo-resources") and helm["operation"] == "install")), "Unsafe atomic release operation")
        checks = row["checks"]
        require(isinstance(checks, list) and checks and all(matches(FIELD, v) for v in checks),
                "Missing post-deploy check identifiers")
        observed = row["observe"]
        require(isinstance(observed, list) and observed and len(set(observed)) == len(observed)
                and all(matches(FIELD, v) for v in observed), "Invalid live observation identities")
        chosen[key] = row
    required = set(requested)
    for key, row in reversed(list(chosen.items())):
        if key in required:
            required.update(row["needs"])
    require(chosen.keys() == required, "Resolved releases must equal requested releases plus prerequisites")
    return chosen


def member_names(plan, rows):
    extra = {"image-selection.json", "image-compatibility.json"} if plan["mode"] == "promotion" and plan["images"] else set()
    return COMMON | extra | {f"releases/{key}/{file}" for key in rows for file in ("chart.tgz", "values.yaml", "rendered.yaml")}


def check_chart(data, expected):
    require(digest(data) == expected["sha256"], "Chart archive SHA-256 differs from plan")
    # Verify every bundled dependency too. Helm can consume unpacked or .tgz subcharts.
    # The budget includes all layers and PAX headers before tarfile processes them.
    remaining = MAX_PLAN
    count = 0
    static = {}
    def chart_root(path):
        # Helm loads dependencies only below the reserved charts/ directory of a chart.
        return len(path.parts) % 2 == 1 and all(part == "charts" for part in path.parts[1::2])

    def inspect(compressed, depth=0, prefix=PurePosixPath()):
        nonlocal remaining, count
        require(depth < 8, "Chart dependency nesting exceeds limit")
        with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as stream:
            raw = stream.read(remaining + 1)
        remaining -= len(raw)
        require(remaining >= 0, "Chart exceeds unpacked size limit")
        names = set()
        charts = {}
        packages = {}
        potential_crds = {}
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
            for item in archive:
                path = PurePosixPath(item.name)
                count += 1
                require(count <= 4096 and path.parts and not path.is_absolute() and
                        ".." not in path.parts and "\\" not in item.name and path not in names and
                        (item.isfile() or item.isdir()), "Chart contains an unsafe or duplicate member")
                names.add(path)
                if item.isfile() and path.name == "Chart.yaml" and chart_root(path.parent):
                    require(item.size < 262144, "Invalid chart metadata member")
                    charts[path.parent] = yaml.safe_load(archive.extractfile(item))
                if (item.isfile() and path.parent.name == "charts" and path.suffix == ".tgz"
                        and chart_root(path.parent.parent)):
                    dep = inspect(archive.extractfile(item).read(), depth + 1, prefix / path.parent)
                    packages.setdefault(path.parent.parent, set()).add(dep["name"])
                if item.isfile() and "crds" in path.parts and path.suffix.lower() in (".yaml", ".yml", ".json"):
                    potential_crds[path] = archive.extractfile(item).read()
        for path, payload in potential_crds.items():
            if any(r / "crds" in path.parents for r in charts):
                source = str(prefix / path)
                require(source not in static, "Duplicate static chart CRD source")
                static[source] = crd_definitions(payload)
        roots = {p.parts[0] for p in names}
        require(len(roots) == 1, "Chart must have one root")
        root = PurePosixPath(next(iter(roots)))
        require(root in charts, "Chart metadata is missing")
        for path, metadata in charts.items():
            require(isinstance(metadata, dict) and metadata.get("name") == path.name,
                    "Unexpected chart name or directory")
            embedded = {dep.name for dep in charts if dep.parent == path / "charts"}
            embedded.update(packages.get(path, ()))
            for dep in metadata.get("dependencies", []):
                require(dep["name"] in embedded or dep.get("alias") in embedded,
                        "Frozen chart is missing a packaged dependency")
        return charts[root]
    meta = inspect(data)
    require(meta["name"] == expected["name"] and str(meta["version"]) == expected["version"],
            "Chart name or version mismatch")
    return static


def check_live(live, rows):
    require(matches(FIELD, live["cluster"]) and rows.keys() == live["releases"].keys(),
            "Incomplete cluster or Helm release observations")
    names = {name for row in rows.values() for name in row["observe"]}
    require(live["objects"].keys() == names, "Incomplete live object observations")
    for state in live["objects"].values():
        require(state is None or matches(FIELD, state["uid"]) and matches(SHA, state["stateSHA256"]),
                "Invalid live object fingerprint")
    require(live["rollbackTargets"].keys() == rows.keys(), "Incomplete live rollback target observations")
    for key, state in live["releases"].items():
        rollback = rows[key]["rollback"]
        status, revision = state["status"], state["revision"]
        require(type(revision) is int, "Invalid prior revision")
        require(rows[key]["helm"]["operation"] == ("install" if status == "absent" else "upgrade"),
                "Frozen Helm operation does not match live release presence")
        if status == "absent":
            require(live["rollbackTargets"][key] is None, "Unexpected stored revision for first adoption")
            require(revision == 0 and state["recordSHA256"] is None and key.endswith("/demo-resources") and
                    rollback == {"strategy": "de-adopt", "revision": 0}, "Unexpected missing release")
        else:
            require(status in ("deployed", "failed") and revision > 0 and matches(SHA, state["recordSHA256"]),
                    "Release state needs operator review")
            require(type(rollback["revision"]) is int and 0 < rollback["revision"] <= revision and
                    matches(SHA, rollback["recordSHA256"]), "Incomplete rollback record")
            target = live["rollbackTargets"][key]
            require(target["revision"] == rollback["revision"] and target["recordSHA256"] == rollback["recordSHA256"]
                    and target["status"] in ("superseded", "deployed"), "Rollback target observation mismatch")
            if rollback["revision"] == revision:
                require(status == "deployed" and rollback["recordSHA256"] == state["recordSHA256"],
                        "Rollback target must be a verified successful record")
            if key.endswith("/demo-resources"):
                strategy = "de-adopt"
            elif key.startswith("opensearch/"):
                strategy = "operator"
            else:
                strategy = "helm-rollback"
            require(rollback["strategy"] == strategy, "Invalid release recovery strategy")


class ManifestLoader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    """Prometheus CRDs use a literal equals sign as an enum value."""


ManifestLoader.add_constructor("tag:yaml.org,2002:value", ManifestLoader.construct_scalar)


def crd_definitions(data):
    result = {}
    for obj in yaml.load_all(data, Loader=ManifestLoader):
        if obj is None:
            continue
        meta = obj["metadata"]
        require(obj["apiVersion"] == "apiextensions.k8s.io/v1" and obj["kind"] == "CustomResourceDefinition"
                and not meta.get("namespace") and meta["name"] not in result, "Invalid or duplicate static CRD")
        result[meta["name"]] = canonical(obj)
    require(result, "Empty static CRD source")
    return result


SOURCE_HEADER = re.compile(rb"^---[ \t]*\r?\n# Source: ([^\r\n]+)\r?\n", re.MULTILINE)


def verify_crd_hook(ordinary, definitions):
    name = "prometheus-crds-upgrade"
    configs = [o for o in ordinary if o["kind"] == "ConfigMap" and "crds.bz2" in (o.get("binaryData") or {})]
    jobs = [o for o in ordinary if o["kind"] == "Job" and o["metadata"]["name"] == name]
    if not definitions and not configs and not jobs:
        return
    require(len(configs) == len(jobs) == 1 and configs[0]["metadata"]["name"] == name,
            "Expected one monitoring CRD hook ConfigMap and Job")
    for obj in (configs[0], jobs[0]):
        metadata = obj["metadata"]
        events = metadata.get("annotations", {}).get("helm.sh/hook", "").split(",")
        require(metadata.get("namespace") == "default" and {"pre-upgrade", "pre-rollback"}.issubset(events),
                "Unexpected monitoring CRD hook scope or events")
    pod = jobs[0]["spec"]["template"]["spec"]
    require(len(pod["initContainers"]) == len(pod["containers"]) == 1, "Unexpected CRD hook container layout")
    unpack, apply = pod["initContainers"][0], pod["containers"][0]
    require(unpack["command"] == ["sh"] and unpack["args"] == ["-c", "bzcat /crds/crds.bz2 > /tmp/crds.yaml"],
            "Unexpected CRD hook unpack command")
    require(apply["command"] == ["kubectl"] and apply["args"] in (["apply", "--server-side", "--filename", "/tmp/crds.yaml"],
            ["apply", "--server-side", "--force-conflicts", "--filename", "/tmp/crds.yaml"]),
            "Unexpected CRD hook apply command")
    def mount(container, path):
        matches = [m for m in container["volumeMounts"] if m["mountPath"].rstrip("/") == path]
        require(len(matches) == 1 and not ({"subPath", "subPathExpr"} & matches[0].keys()), "Unexpected CRD hook mount")
        return matches[0]["name"]
    volumes = {v["name"]: v for v in pod["volumes"]}
    require(len(volumes) == len(pod["volumes"]) == len(unpack["volumeMounts"]) == 2 and len(apply["volumeMounts"]) == 1,
            "Unexpected CRD hook volumes or shadowed file mount")
    source = volumes[mount(unpack, "/crds")]["configMap"]
    dest = mount(unpack, "/tmp")
    require(source["name"] == name and "items" not in source and dest == mount(apply, "/tmp") and
            "emptyDir" in volumes[dest], "CRD hook Job does not apply the verified ConfigMap payload")
    decoder = bz2.BZ2Decompressor()
    try:
        compressed = base64.b64decode(configs[0]["binaryData"]["crds.bz2"], validate=True)
        body = decoder.decompress(compressed, MAX_MEMBER + 1)
    except (binascii.Error, OSError, ValueError):
        raise BundleError("Invalid monitoring CRD hook payload encoding") from None
    require(len(body) <= MAX_MEMBER and decoder.eof and not decoder.unused_data,
            "Invalid or oversized monitoring CRD hook payload")
    require(crd_definitions(body) == definitions, "Monitoring hook payload differs from static chart CRDs")


def transaction_render(row, files):
    """Derive the expected Helm 4 post-renderer stream from a complete frozen render."""
    key = row["id"]
    base = f"releases/{key}"
    static = check_chart(files[base + "/chart.tgz"], row["chart"])
    complete = files[base + "/rendered.yaml"]
    require(not static or row["helm"]["operation"] != "install",
            "Static CRDs are outside the ownership scope of a first companion install")
    admitted = complete
    definitions = {}
    if static:
        # Helm adds archive crds/ files after the post-renderer. Match their full source paths
        # and contents. A CRD from templates/ is an ordinary transaction manifest.
        headers = list(SOURCE_HEADER.finditer(complete))
        require(headers, "Static CRD partition requires source-marked Helm output")
        kept = [complete[:headers[0].start()]]
        seen = {}
        for index, header in enumerate(headers):
            source = header[1].decode()
            end = headers[index+1].start() if index + 1 < len(headers) else len(complete)
            if source not in static:
                kept.append(complete[header.start():end])
                continue
            actual = crd_definitions(complete[header.end():end])
            require(definitions.keys().isdisjoint(actual), "Duplicate CRD identity in the frozen render")
            definitions.update(actual)
            seen.setdefault(source, {}).update(actual)
        require(seen == static, "Frozen static CRDs differ from chart source or contents")
        admitted = b"".join(kept)
    if static or key == "default/prometheus":
        ordinary = [o for o in yaml.load_all(admitted, Loader=ManifestLoader) if o is not None]
        for obj in ordinary:
            if obj["kind"] == "CustomResourceDefinition":
                require(obj["metadata"]["name"] not in definitions, "CRD appears as both static and templated")
        if key == "default/prometheus":
            verify_crd_hook(ordinary, definitions)
    return admitted


def check_render(key, row, rendered, values, images):
    require(isinstance(yaml.load(values, Loader=ManifestLoader), dict), "Expected frozen values mapping")
    namespace, name = key.split("/")
    observed = set(row["observe"])
    count = 0
    workloads = {}
    for obj in yaml.load_all(rendered, Loader=ManifestLoader):
        if obj is None:
            continue
        count += 1
        api, kind, meta = obj["apiVersion"], obj["kind"], obj["metadata"]
        identity = meta["name"]
        namespaces = (meta["namespace"],) if meta.get("namespace") else (namespace, "-")
        require(any(f"{api}:{kind}:{ns}:{identity}" in observed for ns in namespaces),
                "A rendered resource is missing from the live observation scope")
        if kind == "Deployment" and name == "jaeger" and identity in ("jaeger", "jaeger-hotrod"):
            matches = [c["image"] for c in obj["spec"]["template"]["spec"]["containers"] if c["name"] == identity]
            require(len(matches) == 1 and identity not in workloads, "Ambiguous hosted image in frozen render")
            workloads[identity] = matches[0]
    require(count, "Frozen render must include resources")
    if name == "jaeger":
        selected = images["oci" if namespace == "default" else "otel"]
        require(workloads == {"jaeger": selected["jaeger"]["reference"], "jaeger-hotrod": selected["hotrod"]["reference"]},
                "Frozen Jaeger and HotROD manifests do not match the recorded exact image references")


def check_compatibility(files, expected, images):
    candidate = read_json(files["image-selection.json"])
    proof = read_json(files["image-compatibility.json"])
    require(candidate["mode"] == proof["mode"] == "promotion" and
            candidate["configCommit"] == proof["configCommit"] == expected and
            candidate["coreMain"] == proof["coreMain"] and matches(COMMIT, proof["coreMain"]) and
            proof["selectionSHA256"] == digest(files["image-selection.json"]),
            "Image compatibility receipt does not bind this frozen selection")
    for stack, identity in (("oci", "default/jaeger"), ("otel", "jaeger/jaeger")):
        if stack not in images:
            continue
        sources = candidate["sources"]
        projected = {name: {k: src[k] for k in ("reference", "sourceCommit", "digest")}
                     for name, src in sources.items()}
        checked = proof["checks"][stack]
        require(projected == proof["images"][stack] == images[stack] and checked["result"] == "passed" and
                checked["validatedPlatformManifest"] == sources["jaeger"]["platformManifest"] and
                re.fullmatch(r"sha256:[0-9a-f]{64}", checked["validatedPlatformManifest"]) and
                matches(SHA, checked["configSHA256"]) and matches(SHA, checked["uiConfigSHA256"]) and
                checked["renderSHA256"] == digest(files[f"releases/{identity}/rendered.yaml"]),
                "Image compatibility evidence differs from the frozen release")


def validate(files, expected):
    plan = read_json(files["plan.json"])
    rows = selection(plan, expected)
    require(files.keys() == member_names(plan, rows), "Missing or unexpected plan member")
    lock = read_json(files["tools.lock.json"])
    require(set(lock["tools"]) == {"helm", "helmfile", "helm-diff", "kubeconform"}, "Incomplete tool provenance")
    for pin in lock["tools"].values():
        require(pin["repository"] and re.fullmatch(r"v\d+\.\d+\.\d+", pin["version"]) and
                matches(SHA, pin["sha256"].get(pin["version"] + "/linux-amd64")), "Unpinned runner tool")
    require(matches(SHA, lock["kubernetesSchema"]["sha256"]) and
            plan["renderContext"]["kubernetesVersion"] == lock["kubernetesSchema"]["version"],
            "Unpinned or mismatched Kubernetes schema version")
    live = read_json(files["live.json"])
    check_live(live, rows)
    require(live["capabilitiesSHA256"] == digest(canonical(plan["renderContext"])), "Rendering capabilities differ from live observation")
    if plan["mode"] == "adoption":
        require(live["healthy"] is True, "Adoption requires a fresh healthy baseline")
    application_stacks = {stack for stack, key in (("oci", "default/jaeger"), ("otel", "jaeger/jaeger")) if key in rows}
    require(plan["images"].keys() == application_stacks, "Image provenance must match the selected Jaeger releases")
    for stack, images in plan["images"].items():
        require(images.keys() == {"jaeger", "hotrod"}, "Require independent Jaeger and HotROD provenance")
        for image in images.values():
            require(matches(IMAGE, image["reference"]) and matches(COMMIT, image["sourceCommit"]) and
                    re.fullmatch(r"sha256:[0-9a-f]{64}", image["digest"]), "Incomplete image provenance")
            ref = image["reference"]
            if "@" in ref or plan["mode"] == "promotion":
                require(ref.endswith("@" + image["digest"]), "Promotion requires the frozen image digest in the reference")
        if plan["mode"] == "adoption":
            require(live["images"][stack] == images, "Adoption must preserve the observed image references and digests")
    if plan["mode"] == "promotion" and application_stacks:
        check_compatibility(files, expected, plan["images"])
    for key, row in rows.items():
        base = f"releases/{key}"
        transaction_render(row, files)
        check_render(key, row, files[base + "/rendered.yaml"], files[base + "/values.yaml"], plan["images"])
    return plan, live


def build(source, destination, cipher, expected, checkout):
    verify_source(checkout, expected)
    private_directory(source)
    private_directory(destination)
    require(source.resolve() != destination.resolve(), "Separate the input and encrypted output directories")
    plan_bytes = read_member(source, "plan.json")
    plan = read_json(plan_bytes)
    rows = selection(plan, expected)
    names = member_names(plan, rows)
    found = set()
    for root, dirs, entries in os.walk(source):
        for name in dirs:
            require(not (Path(root) / name).is_symlink(), "Source contains a symlinked directory")
        found.update((Path(root) / name).relative_to(source).as_posix() for name in entries)
    require(found == names, "Source contains missing or unexpected files")
    files = {"plan.json": plan_bytes}
    total = len(plan_bytes)
    for name in sorted(names - files.keys()):
        files[name] = read_member(source, name, min(MAX_MEMBER, MAX_PLAN - total))
        total += len(files[name])
    validate(files, expected)
    verify_frozen_source(checkout, expected, files, read_json(files["plan.json"]))
    index = {name: digest(data) for name, data in files.items()}
    body = io.BytesIO()
    with zipfile.ZipFile(body, mode="w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted({**files, "index.json": canonical(index)}.items()):
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o600) << 16
            archive.writestr(info, data)
    require(body.tell() <= MAX_PLAN, "Plan exceeds bundle limit")
    token = cipher.encrypt(body.getvalue())
    sha = digest(token)
    # Publish only complete ciphertext. link refuses to overwrite an existing digest path.
    with tempfile.NamedTemporaryFile(dir=destination) as candidate:
        candidate.write(token)
        candidate.flush()
        os.link(candidate.name, destination / (sha + ".fernet"))
    return {"bundleSHA256": sha, "configurationSHA": expected}


def unseal(path, cipher, approved, expected):
    require(matches(SHA, approved) and matches(COMMIT, expected), "Expected approved bundle and configuration digests")
    token = read_member(path.parent, path.name, MAX_TOKEN)
    require(digest(token) == approved, "Bundle SHA-256 differs from approval")
    plaintext = cipher.decrypt(token)
    require(len(plaintext) <= MAX_PLAN, "Plan exceeds bundle limit")
    # The writer uses plain ZIP without comments, ZIP64 or split volumes. Bound the
    # central directory entry count before ZipFile allocates per-entry structures.
    require(len(plaintext) >= 22 and plaintext[-22:-18] == b"PK\x05\x06", "Invalid private archive")
    disk, central_disk, entries, total, central_size, offset, comment = struct.unpack("<4H2LH", plaintext[-18:])
    require(disk == central_disk == comment == 0 and entries == total <= 6 + len(RELEASES) * 3
            and offset + central_size == len(plaintext) - 22 and central_size <= total * 512,
            "Invalid private archive directory")
    with zipfile.ZipFile(io.BytesIO(plaintext)) as archive:
        items = archive.infolist()
        names = set()
        for item in items:
            name = item.filename
            require(len(name) <= 200 and name not in names and not item.flag_bits & 1 and
                    item.compress_type == zipfile.ZIP_STORED and stat.S_ISREG(item.external_attr >> 16)
                    and item.file_size <= MAX_MEMBER, "Unsafe or duplicated bundle member")
            names.add(name)
        require(len(items) <= 6 + len(RELEASES) * 3 and sum(item.file_size for item in items) <= MAX_PLAN,
                "Archive exceeds bundle limits")
        plan = read_json(archive.read("plan.json"))
        allowed = member_names(plan, selection(plan, expected))
        require(names == allowed | {"index.json"}, "Archive does not match the member allowlist")
        index = read_json(archive.read("index.json"))
        files = {name: archive.read(name) for name in allowed}
        require(index == {name: digest(data) for name, data in files.items()}, "Bundle member digest mismatch")
    plan, live = validate(files, expected)
    return plan, live, files


PRIVATE_FAILURE = "Invalid plan input, private key, bundle, or checkout"


def without_private_errors(call, *args):
    """Replace every failure except BundleError with a fixed message.

    Parser, archive and decryption errors can quote or reference private values. Only this
    library's own code may run inside the call, so no caller's exception is hidden.
    """
    try:
        return call(*args)
    except BundleError:
        raise
    except Exception:
        pass
    # Raised after the handler has ended, so the original exception is not chained to this one.
    raise BundleError(PRIVATE_FAILURE)


def consume(path, cipher, approved, checkout, expected, render_frozen, read_live, reconcile, verify_stack):
    """Called inside the deployment lock. The observer gets identities only, never old hashes.

    Failures while this library reads the bundle raise only BundleError with a fixed message.
    Exceptions raised by the four callbacks propagate unchanged. Those integrations own
    the content of their own errors.
    """
    def verified():
        verify_source(checkout, expected)
        plan, previous, files = unseal(path, cipher, approved, expected)
        verify_frozen_source(checkout, expected, files, read_json(files["plan.json"]))
        return plan, previous, files
    plan, previous, files = without_private_errors(verified)
    files = MappingProxyType(files)
    rendered = render_frozen(read_json(files["plan.json"]), files)
    keys = {row["id"] for row in plan["releases"]}
    require(rendered.keys() == keys and all(rendered[key] == files[f"releases/{key}/rendered.yaml"] for key in keys),
            "Frozen inputs do not reproduce the reviewed renders")
    scope = observation_scope(plan["releases"], previous["cluster"], plan["images"])
    current = read_live(scope)
    require(canonical(current) == canonical(previous), "Live state drifted since approval")
    ids = [row["id"] for row in plan["releases"]]
    if set(ids) != set(previous["releases"]) or len(set(ids)) != len(ids):
        raise ValueError("Selected release scope differs from the verified baseline")
    checked = set()
    def run_release(row, method):
        stack = release_phases.stack_of(row["id"])
        if stack not in checked:
            # Compare only this stack. An earlier successful OCI release may
            # have legitimately advanced its history and changed its objects.
            checked.add(stack)
            selected = [r for r in plan["releases"] if release_phases.stack_of(r["id"]) == stack]
            stack_scope = observation_scope(selected, previous["cluster"], plan["images"])
            if canonical(read_live(stack_scope)) != canonical(baseline_for(stack_scope, previous)):
                raise ValueError(f"{stack} changed after the initial bundle verification")
        prefix = f"releases/{row['id']}/"
        inputs = {name: files[prefix + name] for name in ("chart.tgz", "values.yaml", "rendered.yaml")}
        inputs["postrender.yaml"] = without_private_errors(transaction_render, row, files)
        reconcile(row, method, MappingProxyType(inputs))

    # The approved baseline listed each release's Helm records by name, so its "absent" is confirmed.
    statuses = {key: release["status"] for key, release in previous["releases"].items()}
    return release_phases.run(plan["releases"], statuses, run_release, verify_stack)


# Compatibility for the approval-time drift check, which direct deployment does not use.
# Removed together with this module by the #99 deletion PR.
def observation_scope(rows, cluster, images):
    ids = [row["id"] for row in rows]
    stacks = {stack for stack, key in (("oci", "default/jaeger"), ("otel", "jaeger/jaeger")) if key in ids}
    return {
        "cluster": cluster, "releases": ids, "objects": sorted({key for row in rows for key in row["observe"]}),
        "imageStacks": sorted(stacks.intersection(images)),
        "rollbackRevisions": {row["id"]: row["rollback"]["revision"] for row in rows},
    }


def baseline_for(scope, previous):
    state = {k: previous[k] for k in ("cluster", "capabilitiesSHA256", "healthy")}
    for k in ("releases", "rollbackTargets", "objects"):
        ids = scope["objects" if k == "objects" else "releases"]
        state[k] = {key: previous[k][key] for key in ids}
    if "images" in previous:
        state["images"] = {k: previous["images"][k] for k in scope["imageStacks"]}
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--configuration-sha", required=True)
    parser.add_argument("--key-file", required=True, type=Path)
    commands = parser.add_subparsers(required=True, dest="command")
    creation = commands.add_parser("build")
    creation.add_argument("source", type=Path)
    creation.add_argument("destination", type=Path)
    inspection = commands.add_parser("verify")
    inspection.add_argument("bundle", type=Path)
    inspection.add_argument("--approved-sha", required=True)
    args = parser.parse_args()
    cipher = read_key(args.key_file)
    if args.command == "build":
        result = build(args.source, args.destination, cipher, args.configuration_sha, args.checkout)
    else:
        verify_source(args.checkout, args.configuration_sha)
        _, _, files = unseal(args.bundle, cipher, args.approved_sha, args.configuration_sha)
        verify_frozen_source(args.checkout, args.configuration_sha, files, read_json(files["plan.json"]))
        result = {"bundleSHA256": args.approved_sha, "configurationSHA": args.configuration_sha}
    print(json.dumps(result))


if __name__ == "__main__":
    try:
        main()
    except BundleError as error:
        sys.exit(str(error))
    except Exception:
        # Parsers and decryption failures can carry plaintext from private values. Do not log them.
        sys.exit(PRIVATE_FAILURE)
