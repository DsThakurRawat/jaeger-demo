#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Regenerate the OCI CLI hash file from the pinned versions.

For each exact pin in scripts/demo/oci-cli-constraints.txt this asks PyPI for the
files of that release and writes the pin with the SHA-256 of every one of them to
scripts/demo/oci-cli-hashes.txt, which the workflows install with
`pip install --require-hashes`. Every published file is listed so that one file
serves any runner platform and Python version. Use --check to change nothing and
fail on drift.
"""

import argparse
import json
from pathlib import Path
import re
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
CONSTRAINTS = "scripts/demo/oci-cli-constraints.txt"
HASHES = "scripts/demo/oci-cli-hashes.txt"
INDEX = "https://pypi.org/pypi/{name}/{version}/json"
TIMEOUT = 60
HEADER = """# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
#
# Generated from oci-cli-constraints.txt by scripts/utils/update-oci-cli-hashes.py.
# Do not edit by hand. Each pin lists the SHA-256 of every file PyPI publishes for
# that release, so one file serves any runner platform and Python version.
"""
PIN = re.compile(r"([A-Za-z0-9_.-]+)==([^\s=\\]+)")
HASH = re.compile(r"    --hash=sha256:([0-9a-f]{64})")


class HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse a redirect to anything but HTTPS, so a downgrade cannot supply the digests."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        require_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def require_https(url):
    if not url.startswith("https://"):
        raise ValueError("PyPI URL or redirect is not HTTPS: " + url)
    return url


def fetch(url):
    request = urllib.request.Request(require_https(url), headers={"User-Agent": "jaeger-demo-update-oci-cli-hashes"})
    with urllib.request.build_opener(HttpsOnlyRedirects).open(request, timeout=TIMEOUT) as response:
        return response.read()


def read_pins(path):
    """Return [(name, version)] in file order; anything but comments and exact pins is an error."""
    pins = []
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        match = PIN.fullmatch(line)
        if not match:
            raise ValueError(f"{path.name}: not an exact package==version pin: {line}")
        pins.append((match[1], match[2]))
    return pins


def read_hashes(path):
    """Return [(name, version, [sha256])] from a generated file.

    A requirements file can also carry options such as another index. Only
    comments, exact pins and SHA-256 hashes are accepted here.
    """
    entries = []
    lines = path.read_text().splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if not line or line.startswith("#"):
            continue
        match = PIN.fullmatch(line[:-2]) if line.endswith(" \\") else None
        if not match:
            raise ValueError(f"{path.name}: expected 'package==version \\' but found: {line}")
        digests = []
        more = True
        while more:
            text = lines[index] if index < len(lines) else ""
            more = text.endswith(" \\")
            digest = HASH.fullmatch(text[:-2] if more else text)
            if not digest:
                raise ValueError(f"{path.name}: expected a sha256 hash for {match[1]} but found: {text}")
            digests.append(digest[1])
            index += 1
        entries.append((match[1], match[2], digests))
    return entries


def release_digests(name, version, getter):
    files = json.loads(getter(INDEX.format(name=name, version=version))).get("urls") or []
    digests = sorted({str((entry.get("digests") or {}).get("sha256", "")) for entry in files})
    if not digests or not all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in digests):
        raise ValueError(f"PyPI lists no usable SHA-256 digests for {name}=={version}")
    return digests


def render(pins, getter):
    lines = [HEADER + "\n"]
    for name, version in pins:
        digests = release_digests(name, version, getter)
        lines.append(f"{name}=={version} \\\n" + " \\\n".join("    --hash=sha256:" + digest for digest in digests) + "\n")
    return "".join(lines)


def main(argv=None, root=ROOT, getter=fetch):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="change nothing; exit 1 if the file is stale")
    args = parser.parse_args(argv)
    path = root / HASHES
    try:
        wanted = render(read_pins(root / CONSTRAINTS), getter)
    except ValueError as error:
        print("error: " + str(error), file=sys.stderr)
        return 1
    if path.exists() and path.read_text() == wanted:
        print(f"{HASHES} already matches PyPI for every pin in {CONSTRAINTS}")
        return 0
    if args.check:
        print(f"{HASHES} is stale; run: python3 scripts/utils/update-oci-cli-hashes.py", file=sys.stderr)
        return 1
    path.write_text(wanted)
    print("updated " + HASHES)
    return 0


if __name__ == "__main__":
    sys.exit(main())
