# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Acquire reviewed configuration and public artifacts for the in-cluster preparer."""

import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import urllib.request

import yaml

from deploy import COMPONENTS, ROOT


CONFIG = "https://github.com/jaegertracing/jaeger-demo.git"
CORE = "https://github.com/jaegertracing/jaeger.git"
JAEGER_CHART = "https://github.com/jaegertracing/helm-charts.git"
SHA = re.compile(r"[a-f0-9]{40}")
VERSION = r"[0-9]+\.[0-9]+\.[0-9]+"
CHARTS = (
    ("opensearch/opensearch", "OPENSEARCH_CHART_VERSION", "opensearch", "https://opensearch-project.github.io/helm-charts"),
    ("opensearch/opensearch-dashboards", "OPENSEARCH_DASHBOARDS_CHART_VERSION", "opensearch-dashboards", "https://opensearch-project.github.io/helm-charts"),
    ("otel-demo/otel-demo", "OTEL_DEMO_CHART_VERSION", "opentelemetry-demo", "https://open-telemetry.github.io/opentelemetry-helm-charts"),
    ("default/prometheus", "PROMETHEUS_STACK_CHART_VERSION", "kube-prometheus-stack", "https://prometheus-community.github.io/helm-charts"),
)
DEPS = {"bitnami": "https://charts.bitnami.com/bitnami", "incubator": "https://charts.helm.sh/incubator"}
RESOLVER = "scripts/utils/resolve-demo-snapshot-tags.sh"
GUARDED = (
    "examples/oci/deploy-all.sh", "examples/otel-demo/deploy-all.sh", RESOLVER,
    "scripts/demo/jaeger-chart.sh", "scripts/demo/deploy.py", "scripts/demo/handover.py",
    "scripts/demo/recovery_policy.py", "scripts/demo/scheduled_adapter.py", "scripts/demo/scheduled_bundle.py",
    "scripts/demo/scheduled_inputs.py", "scripts/demo/scheduled_receiver.py", "scripts/demo/scheduled_request.py",
    "scripts/demo/stack_health.py", "scripts/demo/verify_trace.py",
    "scripts/demo/helm-plugins/verify-frozen-manifests/plugin.yaml",
    "scripts/demo/helm-plugins/verify-frozen-manifests/verify.py", "ops/executor/Containerfile",
    "ops/executor/requirements.lock", "docs/all-five-chart-access.json",
)
LOCK = "ops/executor/chart-archives.json"


def require(ok, message):
    if not ok:
        raise ValueError(message)


def public_env(directory):
    return {"PATH": os.environ["PATH"], "HOME": str(directory), "TMPDIR": str(directory), "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ALLOW_PROTOCOL": "https",
            "HELM_CACHE_HOME": str(directory / "cache"), "HELM_CONFIG_HOME": str(directory / "config"),
            "HELM_DATA_HOME": str(directory / "data"), "HELM_NO_PLUGINS": "1", "KUBECONFIG": os.devnull}


def public_command(args, env, timeout=180):
    result = subprocess.run(args, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Public acquisition failed during {Path(str(args[0])).name} {args[1]}")
    return result.stdout


def head(repository, env):
    parts = public_command(["git", "ls-remote", "--exit-code", repository, "refs/heads/main"], env).decode().split()
    require(len(parts) == 2 and SHA.fullmatch(parts[0]) and parts[1] == "refs/heads/main", "Invalid upstream main reference")
    return parts[0]


def main_sha():
    with tempfile.TemporaryDirectory() as directory:
        return head(CONFIG, public_env(Path(directory)))


def fetch_tree(repo, revision, dest, env):
    public_command(["git", "init", "--quiet", str(dest)], env)
    cmd = ["git", "-C", str(dest), "-c", "core.hooksPath=/dev/null"]
    public_command(cmd + ["fetch", "--no-tags", "--depth=1", repo, revision], env)
    commit = public_command(cmd + ["rev-parse", "FETCH_HEAD^{commit}"], env).decode().strip()
    require(SHA.fullmatch(commit), "Unexpected fetched commit")
    return cmd, commit


def config_data(dest, expected, env):
    cmd, fetched = fetch_tree(CONFIG, "refs/heads/main", dest / "config.git", env)
    require(fetched == expected, "Configuration main changed while preparing the slot")
    paths = set(GUARDED) | {LOCK}
    for stack, _, names in COMPONENTS.values():
        prefix = "examples/" + ("oci" if stack == "oci" else "otel-demo") + "/"
        paths.update(prefix + name for name in names)
    root = dest / "inputs"
    for name in sorted(paths):
        listing = public_command(cmd + ["ls-tree", "-l", expected, "--", name], env).decode().split()
        require(len(listing) == 5 and listing[0] in ("100644", "100755") and listing[1] == "blob"
                and listing[4] == name and int(listing[3]) <= 2 * 1024 * 1024, "Missing or unsafe configuration member: " + name)
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(public_command(cmd + ["cat-file", "blob", listing[2]], env))
    return root


def compatible_script(name, text):
    if name not in {"examples/oci/deploy-all.sh", "examples/otel-demo/deploy-all.sh", "scripts/demo/jaeger-chart.sh"}:
        return text
    for _, variable, _, _ in CHARTS:
        if variable == "PROMETHEUS_STACK_CHART_VERSION":
            pattern = rf'(?m)^({variable}="\$\{{{variable}:-){VERSION}(\}}")$'
        else:
            pattern = rf'(?m)^({variable}="){VERSION}(")$'
        text = re.sub(pattern, r'\g<1>VERSION\g<2>', text)
    if name == "scripts/demo/jaeger-chart.sh":
        text = re.sub(r'(?m)^JAEGER_CHART_COMMIT="[a-f0-9]{40}"$', 'JAEGER_CHART_COMMIT="COMMIT"', text)
    return text


def verify_runtime(root):
    for name in GUARDED:
        if compatible_script(name, (ROOT / name).read_text()) != compatible_script(name, (root / name).read_text()):
            raise ValueError(f"Reviewed configuration changed {name}; release the matching executor image")


def pin(root, variable):
    base = "examples/oci" if variable == "PROMETHEUS_STACK_CHART_VERSION" else "examples/otel-demo"
    text = (root / base / "deploy-all.sh").read_text()
    expr = (rf'^{variable}="\$\{{{variable}:-({VERSION})\}}"$' if base == "examples/oci"
            else rf'^{variable}="({VERSION})"$')
    pins = re.findall(expr, text, re.MULTILINE)
    require(len(pins) == 1, "Expected one reviewed pin for " + variable)
    return pins[0]


def checked_archives(paths, locked):
    require(isinstance(locked, dict) and {p.name for p in paths} == set(locked), "Archive set differs from the reviewed checksums")
    result = {}
    for path in paths:
        require(path.is_file() and not path.is_symlink() and 0 < path.stat().st_size <= 32 * 1024 * 1024,
                "Invalid public chart archive")
        data = path.read_bytes()
        require(hashlib.sha256(data).hexdigest() == locked[path.name], "Chart archive differs from approved SHA-256: " + path.name)
        result[path.name] = data
    return result


def charts_for(root, temp, env):
    lock = json.loads((root / LOCK).read_bytes())
    downloads = temp / "charts"
    downloads.mkdir()
    releases = {}
    for release, variable, name, url in CHARTS:
        version = pin(root, variable)
        filename = f"{name}-{version}.tgz"
        require(filename in lock["charts"], "Chart pin needs a reviewed checksum: " + filename)
        public_command(["helm", "pull", name, "--version", version, "--repo", url, "--destination", str(downloads)], env)
        releases[release] = filename
    locked = checked_archives(list(downloads.iterdir()), lock["charts"])
    charts = {key: locked[filename] for key, filename in releases.items()}
    script = (root / "scripts/demo/jaeger-chart.sh").read_text()
    revision, = re.findall(r'^JAEGER_CHART_COMMIT="([a-f0-9]{40})"$', script, re.MULTILINE)
    repo = temp / "upstream"
    git, fetched = fetch_tree(JAEGER_CHART, revision, repo, env)
    require(fetched == revision, "Jaeger chart source differs from reviewed commit")
    public_command(git + ["checkout", "--detach", "--quiet", fetched], env)
    chart = repo / "charts/jaeger"
    require(not any(p.is_symlink() for p in chart.rglob("*")), "Unexpected link in Jaeger chart")
    deps = yaml.safe_load((chart / "Chart.lock").read_bytes())["dependencies"]
    require(all(d["repository"] in DEPS.values() and re.fullmatch(VERSION, d["version"]) for d in deps),
            "Unreviewed Jaeger chart dependency repository or version")
    for name, url in DEPS.items():
        public_command(["helm", "repo", "add", name, url], env)
    public_command(["helm", "dependency", "build", "--skip-refresh", str(chart)], env)
    checked_archives(list((chart / "charts").iterdir()), lock["jaegerDependencies"])
    public_command(["helm", "package", str(chart), "--destination", str(downloads)], env)
    package, = downloads.glob("jaeger-*.tgz")
    charts["default/jaeger"] = charts["jaeger/jaeger"] = package.read_bytes()
    return charts


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Unexpected registry manifest redirect")


def verify_registry_manifest(response, digest):
    data = response.read(8 * 1024 * 1024 + 1)
    require(len(data) <= 8 * 1024 * 1024 and response.headers["Docker-Content-Digest"] == digest
            and "sha256:" + hashlib.sha256(data).hexdigest() == digest, "Registry snapshot digest mismatch")
    manifest = json.loads(data)
    platforms = [item.get("digest", "") for item in manifest.get("manifests", [])
                 if item.get("platform", {}).get("os") == "linux" and item["platform"].get("architecture") == "amd64"]
    require(manifest["schemaVersion"] == 2 and len(platforms) == 1
            and re.fullmatch(r"sha256:[0-9a-f]{64}", platforms[0]),
            "Expected exactly one Linux AMD64 image in the verified snapshot index")
    return platforms[0]



def snapshots(temp, env):
    # The shell resolver selects independently published full commit tags. The
    # publisher's registry must then serve precisely the manifest digest we freeze.
    upstream = head(CORE, env)
    exported = temp / "image-tags"
    public_command(["bash", str(ROOT / RESOLVER)], dict(env, MAIN_SHA=upstream, GITHUB_EVENT_NAME="schedule",
                   GITHUB_ENV=str(exported)), timeout=240)
    tags = dict(line.split("=", 1) for line in exported.read_text().splitlines())
    images = {}
    for role, package in (("jaeger", "jaeger-snapshot"), ("hotrod", "example-hotrod-snapshot")):
        repo = "jaegertracing/" + package
        tag = tags["JAEGER_DEMO_" + role.upper() + "_IMAGE_TAG"]
        require(SHA.fullmatch(tag), "Scheduled snapshot needs a full source commit")
        def download(url, *flags):
            return public_command(["curl", "--fail", "--silent", "--show-error", "--location", "--max-time", "45",
                                   "--proto", "=https", "--proto-redir", "=https", *flags, url], env)
        tag_data = json.loads(download(f"https://hub.docker.com/v2/repositories/{repo}/tags/{tag}/"))
        digest = tag_data["digest"]
        require(tag_data["name"] == tag and re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                and any(x.get("os") == "linux" and x.get("architecture") == "amd64" for x in tag_data["images"]),
                "Snapshot tag is missing its digest or Linux AMD64 image")
        token = json.loads(download("https://auth.docker.io/token?service=registry.docker.io&scope=repository:" + repo + ":pull"))["token"]
        # Keep the short-lived public pull token out of the process arguments.
        url = f"https://registry-1.docker.io/v2/{repo}/manifests/{digest}"
        request = urllib.request.Request(url, headers={"Authorization": "Bearer " + token, "Accept":
            "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=30) as response:
            platform_manifest = verify_registry_manifest(response, digest)
        images[role] = {"repository": "docker.io/" + repo, "tag": tag, "sourceCommit": tag, "digest": digest,
                        "platformManifest": platform_manifest, "reference": f"docker.io/{repo}:{tag}@{digest}"}
    return images


@contextlib.contextmanager
def acquire(configuration_sha):
    require(SHA.fullmatch(configuration_sha), "Expected full reviewed configuration commit")
    with tempfile.TemporaryDirectory(prefix="jaeger-public-inputs-") as folder:
        temp = Path(folder)
        env = public_env(temp)
        root = config_data(temp, configuration_sha, env)
        verify_runtime(root)
        yield root, charts_for(root, temp, env), snapshots(temp, env)
