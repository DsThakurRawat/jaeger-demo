#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Fetch and verify public chart archives against the reviewed SHA-256 lock.

The deployment scripts install charts only from
archives whose bytes equal the digests in scripts/demo/chart-archives.lock.json.

  chart_archives.py pull <chart> <version> <destination-dir>
      Pull <chart> at exactly <version> from its publisher repository into a new
      directory under <destination-dir>, verify it and print the archive path.
  chart_archives.py dependencies <charts-dir>
      Require <charts-dir> to hold exactly the locked Jaeger chart dependencies.
  chart_archives.py state <destination-dir>
      Pull every repository chart of helmfile.yaml through the lock and write
      .helmfile-locked.yaml, the Helmfile state whose charts are those archives.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
VERSION = r"[0-9]+\.[0-9]+\.[0-9]+"
CHARTS = (
    ("opensearch/opensearch", "OPENSEARCH_CHART_VERSION", "opensearch", "https://opensearch-project.github.io/helm-charts"),
    ("opensearch/opensearch-dashboards", "OPENSEARCH_DASHBOARDS_CHART_VERSION", "opensearch-dashboards", "https://opensearch-project.github.io/helm-charts"),
    ("otel-demo/otel-demo", "OTEL_DEMO_CHART_VERSION", "opentelemetry-demo", "https://open-telemetry.github.io/opentelemetry-helm-charts"),
    ("default/prometheus", "PROMETHEUS_STACK_CHART_VERSION", "kube-prometheus-stack", "https://prometheus-community.github.io/helm-charts"),
)
LOCK = "scripts/demo/chart-archives.lock.json"
HELMFILE = "helmfile.yaml"
LOCKED_STATE = ".helmfile-locked.yaml"


def require(ok, message):
    if not ok:
        raise ValueError(message)


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


def load_lock(root=None):
    return json.loads(((root or ROOT) / LOCK).read_bytes())


def pull(chart, version, destination, root=None, run=None):
    """Download one locked chart archive from its publisher and return its verified path."""
    repositories = {name: url for _, _, name, url in CHARTS}
    require(chart in repositories, "Unknown chart: " + chart)
    require(re.fullmatch(VERSION, version), "Expected an exact chart version, not " + repr(version))
    filename = f"{chart}-{version}.tgz"
    locked = load_lock(root)["charts"]
    require(filename in locked, f"{filename} has no reviewed SHA-256 in {LOCK}; "
            "run: python3 scripts/utils/update-chart-archives.py")
    destination = Path(destination)
    require(destination.is_dir(), "Archive destination is not a directory: " + str(destination))
    folder = Path(tempfile.mkdtemp(prefix=chart + "-", dir=destination))
    result = (run or subprocess.run)(["helm", "pull", chart, "--version", version, "--repo", repositories[chart],
                  "--destination", str(folder)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    require(result.returncode == 0, f"helm pull {chart} {version} failed")
    paths = list(folder.iterdir())
    try:
        checked_archives(paths, {filename: locked[filename]})
    except ValueError:
        for path in paths:
            if path.is_file() or path.is_symlink():
                path.unlink()
        raise
    return folder / filename


def dependencies(directory, root=None):
    """Require a built Jaeger chart dependency directory to equal the locked archive set."""
    directory = Path(directory)
    require(directory.is_dir(), "Missing Jaeger chart dependency directory: " + str(directory))
    checked_archives(list(directory.iterdir()), load_lock(root)["jaegerDependencies"])


def sha_of(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def locked_state(root, dest, run=None):
    """Write the derived Helmfile state that installs only lock-verified archives.

    Every release whose chart comes from a repository is pulled with pull(), which
    checks the lock and deletes an archive with another digest. The derived state is
    helmfile.yaml with those releases' chart lines replaced by the verified archive
    paths (relative to root) and the repositories block removed. Any refusal removes
    the previous derived state, so a stale file never survives a failed run.
    """
    import yaml  # Only this command needs PyYAML; pull and dependencies stay standard library.

    root, dest = Path(root).resolve(), Path(dest).resolve()
    target = root / LOCKED_STATE
    temp = target.with_name(target.name + ".tmp")
    for path in (target, temp):
        path.unlink(missing_ok=True)
    try:
        source = (root / HELMFILE).read_text()
        try:
            state = yaml.safe_load(source)
        except yaml.YAMLError as error:
            raise ValueError(f"{HELMFILE} is not valid YAML: {error}") from error
        require(isinstance(state, dict) and set(state) <= {"repositories", "helmDefaults", "releases"},
                f"{HELMFILE} has top-level keys other than repositories, helmDefaults and releases")
        repositories = {entry["name"]: entry["url"] for entry in state.get("repositories") or []}
        by_name = {name: (variable, url) for _, variable, name, url in CHARTS}
        lock = load_lock(root)["charts"]
        plan = []
        for release in state["releases"]:
            chart = release["chart"]
            if chart.startswith(("./", "../", "/")):
                continue
            alias, _, name = chart.partition("/")
            require(name in by_name and alias in repositories, f"Release {release['name']} uses an unlocked chart: {chart}")
            variable, url = by_name[name]
            require(repositories[alias] == url, f"Repository {alias} is not the locked publisher of {name}")
            version = str(release.get("version", ""))
            pinned = pin(root, variable)
            require(version == pinned, f"{name} version {version!r} in {HELMFILE} differs from the deploy-all.sh pin {pinned!r}")
            require(f"{name}-{version}.tgz" in lock, f"{name}-{version}.tgz has no entry in {LOCK}")
            plan.append((chart, name, version))
        missing = set(by_name) - {name for _, name, _ in plan}
        require(not missing, "Locked charts are missing from " + HELMFILE + ": " + ", ".join(sorted(missing)))
        paths = []
        for _, name, version in plan:
            for old in dest.iterdir():
                if old.is_dir() and re.fullmatch(re.escape(name) + "-[a-z0-9_]{8}", old.name):
                    shutil.rmtree(old)
            pulled = pull(name, version, dest, root=root, run=run)
            final = dest / f"{name}-{version}.tgz"
            os.replace(pulled, final)
            pulled.parent.rmdir()
            try:
                paths.append("./" + final.relative_to(root).as_posix())
            except ValueError:
                paths.append(str(final))
        lines, index, in_repositories = [], 0, False
        for line in source.splitlines(keepends=True):
            if re.match(r"repositories:\s*$", line):
                in_repositories = True
                continue
            if in_repositories:
                if line.strip() == "" or line[0] in " \t-#":
                    continue
                in_repositories = False
            match = re.fullmatch(r"(\s+chart: )(\S+)(\s*)", line)
            if match and index < len(plan) and match[2] == plan[index][0]:
                line = match[1] + paths[index] + match[3]
                index += 1
            lines.append(line)
        require(index == len(plan), f"Could not rewrite every repository chart line of {HELMFILE}")
        text = "".join(lines)
        replaced = {chart: path for (chart, _, _), path in zip(plan, paths)}
        expected = {k: v for k, v in state.items() if k != "repositories"}
        expected["releases"] = [dict(r, chart=replaced.get(r["chart"], r["chart"])) for r in state["releases"]]
        require(yaml.safe_load(text) == expected, "Derived state differs from " + HELMFILE + " beyond chart and repositories")
        header = (f"# Generated by scripts/demo/chart_archives.py state; do not edit.\n"
                  f"# {HELMFILE} sha256: {sha_of(root / HELMFILE)}\n"
                  f"# {LOCK} sha256: {sha_of(root / LOCK)}\n")
        temp.write_text(header + text)
        os.replace(temp, target)
    except BaseException:
        for path in (target, temp):
            path.unlink(missing_ok=True)
        raise
    return target


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) == 4 and args[0] == "pull":
            print(pull(*args[1:]))
        elif len(args) == 2 and args[0] == "dependencies":
            dependencies(args[1])
        elif len(args) == 2 and args[0] == "state":
            print(locked_state(ROOT, args[1]))
        else:
            print(__doc__.strip(), file=sys.stderr)
            return 2
    except (ValueError, OSError, KeyError, TypeError, ImportError) as error:
        print("error: " + " ".join(str(error).split()), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
