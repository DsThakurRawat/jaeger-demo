#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""List executor-guarded files that differ between two commits.

The in-cluster executor refuses its daily preparation when a file in
scheduled_inputs.GUARDED differs from the copy in its installed image. This
script applies the same pin normalization to both sides, so a chart version bump
that only edits pin lines is not reported.
"""

import argparse
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

import scheduled_inputs  # noqa: E402


REPO = Path(__file__).resolve().parents[2]


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)


def blob(repo, revision, name):
    """Return the file content at a revision, or None when the file is absent."""
    if git(repo, "cat-file", "-e", f"{revision}:{name}").returncode:
        return None
    result = git(repo, "show", f"{revision}:{name}")
    if result.returncode:
        raise RuntimeError(f"git show {revision}:{name} failed: {result.stderr.strip()}")
    return result.stdout


def changed(repo, base, head):
    for revision in (base, head):
        if git(repo, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}").returncode:
            raise ValueError(f"Unknown commit {revision}")
    found = []
    # Read the list at call time so that scheduled_inputs stays the only copy.
    for name in scheduled_inputs.GUARDED:
        before = blob(repo, base, name)
        after = blob(repo, head, name)
        if before is None and after is None:
            continue
        if before is None or after is None:
            found.append(name)
        elif scheduled_inputs.compatible_script(name, before) != scheduled_inputs.compatible_script(name, after):
            found.append(name)
    return found


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base")
    parser.add_argument("head")
    parser.add_argument("--repo", default=str(REPO))
    args = parser.parse_args(argv)
    try:
        names = changed(args.repo, args.base, args.head)
    except (ValueError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 2
    for name in names:
        print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
