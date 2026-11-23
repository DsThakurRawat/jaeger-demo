#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Regenerate the reviewed chart archive checksums from the pinned chart versions.

For each chart in chart_archives.CHARTS this reads the version pinned in the
deployment scripts, downloads the archive from the publisher, and requires its
SHA-256 to equal the digest the publisher's index.yaml records for that version.
It then rewrites the "charts" map of scripts/demo/chart-archives.lock.json and leaves
"jaegerDependencies" untouched. Use --check to change nothing and fail on drift.
"""

import argparse
import hashlib
import importlib
import json
from pathlib import Path
import sys
import urllib.parse
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[2]
TIMEOUT = 300


def load_inputs():
    sys.path.insert(0, str(ROOT / "scripts/demo"))
    return importlib.import_module("chart_archives")


class HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse redirects to anything but HTTPS, so a downgrade cannot replace both digest and archive."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def require_https(url):
    if not url.startswith("https://"):
        raise ValueError("Chart download URL or redirect is not HTTPS: " + url)
    return url


def fetch(url):
    request = urllib.request.Request(require_https(url), headers={"User-Agent": "jaeger-demo-update-chart-archives"})
    with urllib.request.build_opener(HttpsOnlyRedirects).open(request, timeout=TIMEOUT) as response:
        return response.read()


def load_index(repository, getter):
    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    return yaml.load(getter(require_https(repository.rstrip("/") + "/index.yaml")), Loader=loader)


def index_entry(index, chart, version):
    matches = [e for e in (index.get("entries") or {}).get(chart, []) if str(e.get("version")) == version]
    if len(matches) != 1:
        raise ValueError(f"Expected one index.yaml entry for {chart} {version}, found {len(matches)}")
    return matches[0]


def archive_url(repository, entry, filename):
    urls = entry.get("urls") or [filename]
    return require_https(urllib.parse.urljoin(repository.rstrip("/") + "/", urls[0]))


def resolve(root, charts, pin, getter):
    """Return {filename: digest} for every chart, verified against the publisher index."""
    indexes, result = {}, {}
    for _, variable, chart, repository in charts:
        version = pin(root, variable)
        filename = f"{chart}-{version}.tgz"
        if repository not in indexes:
            indexes[repository] = load_index(repository, getter)
        entry = index_entry(indexes[repository], chart, version)
        recorded = str(entry.get("digest", "")).lower()
        actual = hashlib.sha256(getter(archive_url(repository, entry, filename))).hexdigest()
        if actual != recorded:
            raise ValueError(f"{filename}: archive SHA-256 {actual} differs from the index.yaml digest {recorded}")
        result[filename] = actual
    return dict(sorted(result.items()))


def differences(current, wanted):
    lines = []
    for name in sorted(set(current) | set(wanted)):
        if name not in wanted:
            lines.append(f"remove {name}")
        elif name not in current:
            lines.append(f"add {name} {wanted[name]}")
        elif current[name] != wanted[name]:
            lines.append(f"change {name} {current[name]} -> {wanted[name]}")
    return lines


def main(argv=None, root=ROOT, getter=fetch):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="change nothing; exit 1 and list differences")
    args = parser.parse_args(argv)
    inputs = load_inputs()
    path = root / inputs.LOCK
    lock = json.loads(path.read_text())
    try:
        wanted = resolve(root, inputs.CHARTS, inputs.pin, getter)
    except ValueError as error:
        print("error: " + str(error), file=sys.stderr)
        return 1
    changes = differences(lock["charts"], wanted)
    if not changes:
        print(f"{inputs.LOCK} already matches the four pinned chart archives")
        return 0
    for line in changes:
        print(line)
    if args.check:
        print(f"{inputs.LOCK} is stale; run: python3 scripts/utils/update-chart-archives.py", file=sys.stderr)
        return 1
    lock["charts"] = wanted
    path.write_text(json.dumps(lock, indent=2) + "\n")
    print("updated " + inputs.LOCK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
