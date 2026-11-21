# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Require a CHANGELOG.md entry for a pull request and check the file's structure.

A change needs an entry unless every changed file is exempt (tests and the
changelog itself) or the pull request carries the skip label. An entry is an
added top-level bullet under a dated heading and a known category. Whenever
CHANGELOG.md changes, its date and category headings are validated. The rules
are documented in CONTRIBUTING.md under "Changelog".
"""

import argparse
import datetime
import fnmatch
import os
from pathlib import Path
import re
import subprocess
import sys


REPO = Path(__file__).resolve().parents[2]
CHANGELOG = "CHANGELOG.md"
SKIP_LABEL = "skip-changelog"
CATEGORIES = ("Added", "Changed", "Fixed", "Removed", "Security")
# Files that never need an entry on their own. Anything else does, unless the
# skip label says the change is not notable.
EXEMPT = (CHANGELOG, "*_test.py", "*.test.sh")
DATE_HEADING = re.compile(r"^## (\d{4}-\d{2}-\d{2})$")


class CheckError(Exception):
    pass


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise CheckError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def changed_files(repo, base, head):
    return [name for name in git(repo, "diff", "--name-only", "--no-renames", base, head).splitlines() if name]


def exempt(name):
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in EXEMPT)


def added_lines(repo, base, head):
    """Return the head line numbers (1-based) that the diff adds to the changelog."""
    added = set()
    line = 0
    for text in git(repo, "diff", "-U0", "--no-renames", base, head, "--", CHANGELOG).splitlines():
        hunk = re.match(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", text)
        if hunk:
            line = int(hunk.group(1))
        elif text.startswith("+") and not text.startswith("+++"):
            added.add(line)
            line += 1
    return added


def parse(text):
    """Return (errors, sections) where sections maps a line number to its (date, category)."""
    errors = []
    sections = {}
    date = category = None
    dates = []
    seen = set()
    for number, line in enumerate(text.splitlines(), start=1):
        if line.startswith("## "):
            match = DATE_HEADING.match(line)
            if not match:
                errors.append(f"line {number}: expected '## YYYY-MM-DD', found {line!r}")
                date = category = None
                continue
            try:
                datetime.date.fromisoformat(match.group(1))
            except ValueError:
                errors.append(f"line {number}: {match.group(1)} is not a valid date")
            if dates and match.group(1) >= dates[-1]:
                errors.append(f"line {number}: {match.group(1)} must be earlier than {dates[-1]} (newest first, no duplicates)")
            dates.append(match.group(1))
            date, category, seen = match.group(1), None, set()
        elif line.startswith("### "):
            name = line[4:].strip()
            if date is None:
                errors.append(f"line {number}: category {name!r} is not under a date heading")
            elif name not in CATEGORIES:
                errors.append(f"line {number}: unknown category {name!r}; use one of {', '.join(CATEGORIES)}")
            elif name in seen:
                errors.append(f"line {number}: category {name!r} appears twice under {date}")
            else:
                seen.add(name)
                category = name
        elif line.startswith("- ") and date is not None:
            if category is None:
                errors.append(f"line {number}: entry under {date} is not under a category heading")
            else:
                sections[number] = (date, category)
    return errors, sections


def check(repo, base, head, skip=False, pr=None, today=None):
    """Return (errors, warnings) for the change from base to head."""
    for revision in (base, head):
        try:
            git(repo, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}")
        except CheckError:
            raise CheckError(f"Unknown commit {revision}") from None
    files = changed_files(repo, base, head)
    errors, warnings = [], []
    entries = []
    text = ""
    if CHANGELOG in files:
        try:
            text = git(repo, "show", f"{head}:{CHANGELOG}")
        except CheckError:
            return [f"{CHANGELOG} was deleted"], warnings
        structure, sections = parse(text)
        errors.extend(f"{CHANGELOG} {message}" for message in structure)
        try:
            before = len(parse(git(repo, "show", f"{base}:{CHANGELOG}"))[1])
        except CheckError:
            before = 0
        # Count entries rather than edited lines: rewording or moving an existing
        # entry changes its bullet line but does not record this change.
        if len(sections) > before:
            entries = [sections[number] for number in sorted(added_lines(repo, base, head)) if number in sections]
    notable = [name for name in files if not exempt(name)]
    if notable and not skip and not entries:
        listed = ", ".join(notable[:5]) + (f" and {len(notable) - 5} more" if len(notable) > 5 else "")
        errors.append(
            f"No {CHANGELOG} entry for changes to {listed}. Add a bullet under the merge date and a category "
            f"(see CONTRIBUTING.md#changelog), or apply the '{SKIP_LABEL}' label if no operator or "
            "contributor would notice this change.")
    if entries:
        today = today or datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        stale = sorted({date for date, _ in entries if date != today})
        if stale:
            warnings.append(
                f"New {CHANGELOG} entries are under {', '.join(stale)}, not today ({today} UTC). "
                "Move them to the merge date if this merges on another day.")
        # The link usually sits on a continuation line, so search the whole file.
        if pr is not None and f"/pull/{pr})" not in text:
            warnings.append(f"No {CHANGELOG} entry links this pull request (#{pr}) yet.")
    return errors, warnings


def report(kind, message):
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{kind}::{message}")
    else:
        print(f"{kind}: {message}", file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("base")
    parser.add_argument("head")
    parser.add_argument("--repo", default=str(REPO))
    parser.add_argument("--skip", action="store_true", help=f"the pull request has the '{SKIP_LABEL}' label")
    parser.add_argument("--pr", type=int, help="pull request number, to warn when no entry links it")
    args = parser.parse_args(argv)
    try:
        errors, warnings = check(args.repo, args.base, args.head, skip=args.skip, pr=args.pr)
    except CheckError as error:
        print(error, file=sys.stderr)
        return 2
    for message in warnings:
        report("warning", message)
    for message in errors:
        report("error", message)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
