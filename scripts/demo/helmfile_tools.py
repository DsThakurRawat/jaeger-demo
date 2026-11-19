#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Run adoption tools from the reviewed release archives in helmfile-tools.lock.json."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

LOCK = Path(__file__).with_name("helmfile-tools.lock.json")
CACHE = LOCK.parents[2] / ".tools/helmfile-archives"
PLATFORMS = ("darwin-amd64", "darwin-arm64", "linux-amd64", "linux-arm64")
ARCHIVES = {
    "helm": "helm-{version}-{platform}.tar.gz",
    "helmfile": "helmfile_{number}_{os}_{arch}.tar.gz",
    "helm-diff": "diff-{number}-{diff_os}-{arch}.tgz",
    "kubeconform": "kubeconform-{platform}.tar.gz",
}
CHECKSUMS = {
    "helm": "{archive}.sha256sum",
    "helmfile": "helmfile_{number}_checksums.txt",
    "helm-diff": "helm-diff_{number}_checksums.txt",
    "kubeconform": "CHECKSUMS",
}


def host():
    arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine(), platform.machine())
    system = f"{platform.system().lower()}-{arch}"
    if system not in PLATFORMS:
        raise ValueError(f"Unsupported tool platform {system}; supported: {', '.join(PLATFORMS)}")
    return system


def release(tool, entry, system):
    version = entry["version"]
    if not re.fullmatch(r"v\d+\.\d+\.\d+", version):
        raise ValueError(f"{tool} requires an exact release version")
    os_name, arch = system.split("-")
    number = version[1:]
    archive = ARCHIVES[tool].format(version=version, platform=system, number=number, os=os_name,
                                    arch=arch, diff_os="macos" if os_name == "darwin" else os_name)
    base = f"https://github.com/{entry['repository']}/releases/download/{version}"
    if tool == "helm":
        base = "https://get.helm.sh"
    checksums = CHECKSUMS[tool].format(archive=archive, number=number)
    return base, archive, checksums


def download(url, target):
    subprocess.run(["curl", "--fail", "--silent", "--show-error", "--location", "--retry", "3",
                    "--max-time", "300", "--proto", "=https", "--proto-redir", "=https",
                    "--output", str(target), url], check=True)


def verify(stream, expected, archive):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    if digest.hexdigest() != expected:
        raise ValueError(f"{archive} does not match the reviewed SHA-256 {expected}")
    stream.seek(0)


def cached_download(url, digest, cache):
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError(f"Expected a release SHA-256 for {url}")
    cached = cache / digest
    if not cached.exists():
        with tempfile.TemporaryDirectory(dir=cache) as staging:
            candidate = Path(staging) / "archive"
            download(url, candidate)
            with candidate.open("rb") as stream:
                verify(stream, digest, url)
            os.replace(candidate, cached)
    return cached


def install(tool, entry, system, cache, directory):
    base, archive, _ = release(tool, entry, system)
    key = f"{entry['version']}/{system}"
    digest = entry["sha256"].get(key, "")
    if not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError(f"Missing reviewed SHA-256 for {tool} {key}")
    cached = cached_download(f"{base}/{archive}", digest, cache)

    members = {f"{system}/helm" if tool == "helm" else tool: (f"bin/{tool}", 0o755)}
    if tool == "helm-diff":
        members = {"diff/plugin.yaml": ("plugins/diff/plugin.yaml", 0o644),
                   "diff/bin/diff": ("plugins/diff/bin/diff", 0o755)}
    with cached.open("rb") as stream:
        verify(stream, digest, archive)
        with tarfile.open(fileobj=stream, mode="r:gz") as tar:
            for name, (relative, mode) in members.items():
                item = tar.getmember(name)
                if not item.isfile():
                    raise ValueError(f"Expected a regular file: {archive} / {name}")
                target = directory / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(item) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output)
                target.chmod(mode)


def refreshed_checksums(tool, entry):
    sums = {}
    with tempfile.TemporaryDirectory() as directory:
        for system in PLATFORMS:
            base, archive, checksums = release(tool, entry, system)
            manifest = Path(directory) / checksums
            if not manifest.exists():
                download(f"{base}/{checksums}", manifest)
            records = [row.split() for row in manifest.read_text().splitlines()]
            matches = [row[0] for row in records if len(row) == 2 and row[1] == archive]
            if len(matches) != 1 or not re.fullmatch(r"[a-f0-9]{64}", matches[0]):
                raise ValueError(f"Missing or ambiguous digest for {archive} in {checksums}")
            sums[f"{entry['version']}/{system}"] = matches[0]
    return sums


def check_versions(tools, env):
    commands = {"helm": ["helm", "version", "--template", "{{.Version}}"],
                "helmfile": ["helmfile", "--version"], "helm-diff": ["helm", "diff", "version"],
                "kubeconform": ["kubeconform", "-v"]}
    for tool, command in commands.items():
        output = subprocess.check_output(command, env=env, text=True).strip()
        version = tools[tool]["version"][1:]
        if output not in (version, "v" + version, "helmfile version " + version,
                          "helmfile version v" + version):
            raise ValueError(f"Unexpected {tool} version: {output!r} (expected {version})")
        print(f"{tool}: {output}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="operation", required=True)
    subs.add_parser("check", help="Verify archives and launch each pinned tool")
    subs.add_parser("refresh", help="Refresh an updated version's official checksums").add_argument(
        "tool", choices=ARCHIVES)
    subs.add_parser("run", help="Run a command with the pinned tools").add_argument(
        "command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    document = json.loads(LOCK.read_text())
    tools = document["tools"]
    if tools.keys() != ARCHIVES.keys():
        raise ValueError("The lock must cover all four adoption tools")
    if args.operation == "refresh":
        tools[args.tool]["sha256"] = refreshed_checksums(args.tool, tools[args.tool])
        LOCK.write_text(json.dumps(document, indent=2) + "\n")
        return 0
    command = getattr(args, "command", [])
    if command[:1] == ["--"]:
        command = command[1:]
    if args.operation == "run" and not command:
        parser.error("run requires a command")
    system = host()
    CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temp:
        directory = Path(temp)
        for tool, entry in tools.items():
            install(tool, entry, system, CACHE, directory)
        guard = LOCK.parent / "helm-plugins" / "verify-frozen-manifests"
        shutil.copytree(guard, directory / "plugins" / guard.name)
        env = dict(os.environ, PATH=str(directory / "bin") + os.pathsep + os.environ["PATH"],
                   HELM_PLUGINS=str(directory / "plugins"))
        env.setdefault("HELM_CACHE_HOME", str(CACHE.parent / "helm-cache"))
        if args.operation == "check":
            check_versions(tools, env)
            return 0
        return subprocess.run(command, env=env).returncode


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, tarfile.TarError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
