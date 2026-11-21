#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Resolve public snapshot images and validate a promotion against both frozen Jaeger renders."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

import yaml


ROOT = Path(__file__).resolve().parents[2]
CORE = "https://github.com/jaegertracing/jaeger.git"
REPOS = {"jaeger": "jaegertracing/jaeger-snapshot", "hotrod": "jaegertracing/example-hotrod-snapshot"}
STACKS = {"oci": "default", "otel": "jaeger"}
COMMIT = re.compile(r"[a-f0-9]{40}")
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
TAG = re.compile(r"[\w][\w.-]{0,127}", re.ASCII)
INDEX = {"application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json"}
MANIFEST = {"application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def run(args, **kwargs):
    return subprocess.check_output(args, text=True, timeout=kwargs.pop("timeout", 90), **kwargs)


class RegistryRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require(urlsplit(newurl).scheme == "https", "Registry redirect requires HTTPS")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urlsplit(req.full_url).netloc != urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


def fetch(url, headers=None):
    req = Request(url, headers=headers or {})
    with build_opener(RegistryRedirect()).open(req, timeout=30) as resp:
        body = resp.read(8 * 1024 * 1024 + 1)
        require(len(body) <= 8 * 1024 * 1024, "Registry metadata exceeds limit")
        return body, resp.headers


def tag_commit(repo, tag, digest, labels, lookup):
    if COMMIT.fullmatch(tag):
        commit = tag
    else:
        commit = (labels or {}).get("org.opencontainers.image.revision", "")
        if not COMMIT.fullmatch(commit):
            base = f"https://hub.docker.com/v2/repositories/{repo}/tags/"
            url = base + "?page_size=100&ordering=last_updated"
            visited = set()
            candidates = set()
            while url:
                parsed = urlsplit(url)
                require(parsed.scheme == "https" and parsed.netloc == "hub.docker.com"
                        and parsed.path == urlsplit(base).path and url not in visited, "Invalid Hub pagination")
                visited.add(url)
                page = json.loads(fetch(url)[0])
                candidates.update(r["name"] for r in page["results"] if r.get("digest") == digest and COMMIT.fullmatch(r["name"]))
                require(len(candidates) <= 1, "Ambiguous source commit tag")
                url = page.get("next")
            commit = next(iter(candidates), "")
        require(COMMIT.fullmatch(commit), f"No verifiable source commit for {repo}:{tag}")
        require(lookup(commit)[1] == digest, "Image digest differs from source commit tag")
    label = (labels or {}).get("org.opencontainers.image.revision")
    require(not label or label == commit, "Image revision label differs from source commit tag")
    return commit


def registry_image(repo, tag, frozen=None, *, reference_digest=None):
    require(repo in REPOS.values() and TAG.fullmatch(tag), "Unexpected snapshot repository or tag")
    token_query = urlencode({"service": "registry.docker.io", "scope": f"repository:{repo}:pull"})
    token = json.loads(fetch("https://auth.docker.io/token?" + token_query)[0])["token"]
    root = f"https://registry-1.docker.io/v2/{repo}/"
    headers = {"Authorization": "Bearer " + token, "Accept": ", ".join(sorted(INDEX | MANIFEST))}

    def manifest(ref):
        data, fields = fetch(root + "manifests/" + ref, headers)
        digest = "sha256:" + sha(data)
        require(fields.get("Docker-Content-Digest") == digest and (not DIGEST.fullmatch(ref) or ref == digest),
                "Registry manifest digest mismatch")
        obj = json.loads(data)
        require(obj.get("schemaVersion") == 2 and obj.get("mediaType") in INDEX | MANIFEST, "Unexpected manifest type")
        return obj, digest

    require(reference_digest is None or DIGEST.fullmatch(reference_digest), "Invalid supplied image digest")
    image, digest = manifest(frozen["digest"] if frozen else reference_digest or tag)
    platform_digest = digest
    if image["mediaType"] in INDEX:
        linux = [m for m in image["manifests"] if m.get("platform", {}).get("os") == "linux"
                 and m["platform"].get("architecture") == "amd64"]
        require(len(linux) == 1 and DIGEST.fullmatch(linux[0]["digest"]), "Missing or ambiguous linux/amd64 image")
        image, platform_digest = manifest(linux[0]["digest"])
    require(image["mediaType"] in MANIFEST, "Expected a platform image manifest")
    cfg = image["config"]["digest"]
    require(DIGEST.fullmatch(cfg), "Invalid image configuration digest")
    data = fetch(root + "blobs/" + cfg, headers)[0]
    require("sha256:" + sha(data) == cfg, "Image configuration content digest mismatch")
    config = json.loads(data)
    require((config["os"], config["architecture"]) == ("linux", "amd64"), "Expected Linux AMD64")
    labels = config["config"].get("Labels") or {}
    if frozen:
        # Check only immutable content during compatibility verification. The
        # source tag can move between selection and approval.
        require(platform_digest == frozen["platformManifest"], "Frozen platform manifest differs from its index")
        commit = frozen["sourceCommit"]
        require(not COMMIT.fullmatch(tag) or commit == tag, "Frozen source differs from commit tag")
        require(not labels.get("org.opencontainers.image.revision") or labels["org.opencontainers.image.revision"] == commit,
                "Frozen revision differs from image configuration")
    else:
        commit = tag_commit(repo, tag, digest, labels, manifest)
        if reference_digest and COMMIT.fullmatch(tag) and not labels.get("org.opencontainers.image.revision"):
            require(manifest(tag)[1] == digest, "Pinned image digest differs from its claimed commit tag")
    return {"repository": repo, "tag": tag, "sourceCommit": commit, "digest": digest,
            "platformManifest": platform_digest, "reference": f"docker.io/{repo}:{tag}@{digest}"}


def config_commit():
    value = run(["git", "-C", str(ROOT), "rev-parse", "HEAD"]).strip()
    require(COMMIT.fullmatch(value), "Cannot bind configuration commit")
    return value


def resolve(event, overrides):
    head = run(["git", "ls-remote", CORE, "refs/heads/main"]).strip().split()
    require(len(head) == 2 and COMMIT.fullmatch(head[0]) and head[1] == "refs/heads/main", "Unexpected core main")
    env = {"PATH": os.environ["PATH"], "MAIN_SHA": head[0], "GITHUB_EVENT_NAME": event}
    for name, tag in overrides.items():
        if tag is not None:
            require(TAG.fullmatch(tag), f"Invalid explicit {name} tag")
            env[f"JAEGER_DEMO_{name.upper()}_IMAGE_TAG"] = tag
    with tempfile.TemporaryDirectory() as temp:
        exported = Path(temp) / "resolved"
        run(["bash", str(ROOT / "scripts/utils/resolve-demo-snapshot-tags.sh")], env=dict(env, GITHUB_ENV=str(exported)))
        tags = dict(line.split("=", 1) for line in exported.read_text().splitlines())
    for name, tag in overrides.items():
        # An explicit tag selects exactly that image. On a scheduled event the shared resolver
        # can substitute the newest published SHA for a missing tag. Only an unspecified image
        # may use that fallback, and an explicit latest must become a commit tag.
        resolved = tags[f"JAEGER_DEMO_{name.upper()}_IMAGE_TAG"]
        if tag == "latest":
            require(COMMIT.fullmatch(resolved), f"Explicit latest {name} tag did not resolve to a published commit tag")
        elif tag is not None:
            require(resolved == tag, f"Explicit {name} tag is not published. Refusing to select a different image")
    sources = {name: registry_image(repo, tags[f"JAEGER_DEMO_{name.upper()}_IMAGE_TAG"]) for name, repo in REPOS.items()}
    images = {stack: {name: {"repository": image["repository"], "tag": image["tag"] + "@" + image["digest"],
                             "pullPolicy": "Always"} for name, image in sources.items()} for stack in STACKS}
    return {"mode": "promotion", "configCommit": config_commit(), "coreMain": head[0],
            "platform": "linux/amd64", "sources": sources, "images": images}


def named(items, predicate, label):
    matches = [item for item in items if predicate(item)]
    require(len(matches) == 1, f"Expected exactly one {label}")
    return matches[0]


def config_inputs(render, stack, sources):
    ns = STACKS[stack]
    objs = [o for o in yaml.safe_load_all(render) if o]

    def resource(kind, name):
        return named(objs, lambda o: o["kind"] == kind and o["metadata"]["name"] == name
                     and o["metadata"].get("namespace", ns) == ns, f"{ns}/{kind}/{name}")

    spec = resource("Deployment", "jaeger")["spec"]["template"]["spec"]
    backend = named(spec["containers"], lambda c: c["name"] == "jaeger", "Jaeger container")
    hotpod = resource("Deployment", "jaeger-hotrod")["spec"]["template"]["spec"]
    hotrod = named(hotpod["containers"], lambda c: c["name"] == "jaeger-hotrod", "HotROD container")
    require(backend["image"] == sources["jaeger"]["reference"] and hotrod["image"] == sources["hotrod"]["reference"],
            f"Rendered {stack} images differ from the selected digests")
    require(not backend.get("command") and backend["args"] == ["--config", "/etc/jaeger/user-config.yaml"],
            "Unexpected Jaeger entrypoint or config arguments")
    require({m["mountPath"] for m in backend["volumeMounts"]} == {"/etc/jaeger", "/etc/jaeger/ui-config"},
            "Unsupported Jaeger configuration volume mounts")
    files = {}
    for directory, filename in (("/etc/jaeger", "user-config.yaml"), ("/etc/jaeger/ui-config", "ui-config.json")):
        mount = named(backend["volumeMounts"], lambda m: m["mountPath"] == directory, directory)
        require(not ({"subPath", "subPathExpr"} & mount.keys()), "Unexpected Jaeger config subpath")
        volume = named(spec["volumes"], lambda v: v["name"] == mount["name"], mount["name"])["configMap"]
        require(not volume.get("items"), "Unexpected projection of config keys")
        cfg = resource("ConfigMap", volume["name"])
        require(cfg["metadata"].get("namespace") == ns, "Rendered ConfigMap must identify its stack namespace")
        files[filename] = cfg["data"][filename]
    json.loads(files["ui-config.json"])
    require(not backend.get("envFrom"), "Cannot validate a process with unresolved environment sources")
    env = []
    for entry in backend.get("env", []):
        require(set(entry) == {"name", "value"} and isinstance(entry["value"], str), "Unresolved Jaeger environment")
        env.extend(["--env", entry["name"] + "=" + entry["value"]])
    return files, env


def validate(selection_bytes, selection_sha, renders, runtime):
    require(sha(selection_bytes) == selection_sha, "Selection differs from the reviewed SHA-256")
    candidate = json.loads(selection_bytes)
    require(candidate["mode"] == "promotion" and candidate["platform"] == "linux/amd64"
            and COMMIT.fullmatch(candidate["coreMain"]) and candidate["configCommit"] == config_commit(),
            "Invalid promotion input or changed configuration revision")
    sources = candidate["sources"]
    require(sources.keys() == REPOS.keys() and candidate["images"].keys() == STACKS.keys(), "Incomplete image selection")
    public = {}
    for name, image in sources.items():
        repo = REPOS[name]
        require(image["repository"] == repo and TAG.fullmatch(image["tag"]) and COMMIT.fullmatch(image["sourceCommit"])
                and DIGEST.fullmatch(image["digest"]) and DIGEST.fullmatch(image["platformManifest"])
                and image["reference"] == f"docker.io/{repo}:{image['tag']}@{image['digest']}", "Invalid snapshot reference")
        public[name] = {k: image[k] for k in ("reference", "sourceCommit", "digest")}
    expected = {name: {"repository": i["repository"], "tag": i["tag"] + "@" + i["digest"], "pullPolicy": "Always"}
                for name, i in sources.items()}
    require(all(c == expected for c in candidate["images"].values()), "Unexpected Helmfile image state")
    inputs = {stack: config_inputs(renders[stack], stack, sources) for stack in STACKS}
    for image in sources.values():
        require(registry_image(image["repository"], image["tag"], frozen=image) == image,
                "Frozen image identity does not match digest-addressed registry content")
    # Pull the recorded index by digest, letting the runtime verify its AMD64
    # child. Use this same index reference for both binary invocations.
    image = "docker.io/" + REPOS["jaeger"] + "@" + sources["jaeger"]["digest"]
    run([runtime, "pull", "--platform", "linux/amd64", image], timeout=360)
    checks = {}
    for stack, (files, env) in inputs.items():
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "etc-jaeger"
            (config / "ui-config").mkdir(parents=True, mode=0o755)
            for name, data in files.items():
                target = config / ("ui-config" if name == "ui-config.json" else "") / name
                target.write_bytes(data.encode())
                target.chmod(0o644)
            config.chmod(0o755)
            (config / "ui-config").chmod(0o755)
            try:
                run([runtime, "run", "--rm", "--pull=never", "--platform", "linux/amd64", "--network", "none",
                     "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", "10001:10001",
                     "--mount", f"type=bind,source={config},target=/etc/jaeger,readonly", *env, image,
                     "validate", "--config", "/etc/jaeger/user-config.yaml"], timeout=90, stderr=subprocess.PIPE)
            except subprocess.SubprocessError as error:
                # The CLI arguments and binary error may contain rendered environment values.
                raise ValueError(f"Jaeger rejected the {stack} configuration or the local runtime failed") from error
        checks[stack] = {"renderSHA256": sha(renders[stack]), "configSHA256": sha(files["user-config.yaml"].encode()),
                         "uiConfigSHA256": sha(files["ui-config.json"].encode()), "result": "passed",
                         "validatedPlatformManifest": sources["jaeger"]["platformManifest"]}
    return {"mode": "promotion", "configCommit": candidate["configCommit"], "coreMain": candidate["coreMain"],
            "selectionSHA256": sha(selection_bytes), "images": {stack: public for stack in STACKS}, "checks": checks}


def publish(path, result):
    with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", delete=False) as output:
        temp = Path(output.name)
        try:
            json.dump(result, output, sort_keys=True, indent=2)
            output.write("\n")
            output.flush()
            os.link(temp, path)
        finally:
            temp.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    resolve_parser = commands.add_parser("resolve", help="write a digest-pinned state-values JSON")
    resolve_parser.add_argument("--event", choices=("schedule", "workflow_dispatch", "push"), required=True)
    resolve_parser.add_argument("--jaeger-tag")
    resolve_parser.add_argument("--hotrod-tag")
    validate_parser = commands.add_parser("validate", help="test both renders and publish a compatibility receipt")
    validate_parser.add_argument("--selection", required=True, type=Path)
    validate_parser.add_argument("--selection-sha256", required=True, help="digest of the reviewed selection file bytes")
    validate_parser.add_argument("--oci-render", required=True, type=Path)
    validate_parser.add_argument("--otel-render", required=True, type=Path)
    validate_parser.add_argument("--runtime", choices=("docker", "podman"), default="docker")
    for subparser in (resolve_parser, validate_parser):
        subparser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    require(not args.output.exists() and not args.output.is_symlink(), "Choose a new output path")
    if args.command == "resolve":
        result = resolve(args.event, {"jaeger": args.jaeger_tag, "hotrod": args.hotrod_tag})
    else:
        result = validate(args.selection.read_bytes(), args.selection_sha256, {"oci": args.oci_render.read_bytes(),
                          "otel": args.otel_render.read_bytes()}, args.runtime)
    publish(args.output, result)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
