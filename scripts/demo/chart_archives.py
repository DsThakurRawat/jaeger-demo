#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Fetch and verify public chart archives against the reviewed SHA-256 lock.

The deployment scripts and the in-cluster executor install charts only from
archives whose bytes equal the digests in ops/executor/chart-archives.json.

  chart_archives.py pull <chart> <version> <destination-dir>
      Pull <chart> at exactly <version> from its publisher repository into a new
      directory under <destination-dir>, verify it and print the archive path.
  chart_archives.py dependencies <charts-dir>
      Require <charts-dir> to hold exactly the locked Jaeger chart dependencies.
"""

import hashlib
import json
from pathlib import Path
import re
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
LOCK = "ops/executor/chart-archives.json"


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


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    try:
        if len(args) == 4 and args[0] == "pull":
            print(pull(*args[1:]))
        elif len(args) == 2 and args[0] == "dependencies":
            dependencies(args[1])
        else:
            print(__doc__.strip(), file=sys.stderr)
            return 2
    except (ValueError, OSError) as error:
        print("error: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
