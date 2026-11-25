# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Read the bash blocks of the Helmfile adoption runbook in docs/cutover.md.

Each bash block of the section carries a marker on the line before its opening fence:

- `<!-- rehearse: <id> -->`: the kind rehearsal (scripts/demo/rehearsals/helmfile-runbook) runs
  it verbatim. It reads the blocks from the document at run time, so it holds no copy of them.
- `<!-- not-rehearsed: <reason> -->`: kind cannot run it, for the reason given.
- `<!-- session: <reason> -->`: it sets shell state (variables, umask, the pinned shell) in the
  operator's own shell and touches no cluster.

A block that does work is one `bash -euo pipefail <<'EOF'` command, so that it stops at its first
failure even when pasted into an interactive shell. Its first lines guard every documented
variable it reads with `: "${NAME:?}"`, so an empty one stops it before anything runs.
"""

from collections import namedtuple
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs/cutover.md"
SECTION = "## Helmfile adoption"
FENCE = re.compile(r"^(\s*)```(\S*)\s*$")
MARKER = re.compile(r"^\s*<!-- (rehearse|not-rehearsed|session): (.*?) -->\s*$")
IDENTIFIER = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
STRICT_OPEN, STRICT_CLOSE = "bash -euo pipefail <<'EOF'", "EOF"
# The variables the runbook documents. A block reads no other variable of the operator's shell.
VARIABLES = frozenset({"REVIEWED_COMMIT", "ADMIN_KUBECONFIG", "ADMIN_CONTEXT", "MANUAL_USER_OCID",
                       "SCHEDULED_USER_OCID", "RECORD", "KUBECONFIG", "CONTEXT", "ns", "release", "REVISION",
                       "RUN_ID", "RUN_ATTEMPT", "STEP", "FROM"})
GUARD = re.compile(r"\$\{(\w+):[?=]")
REFERENCE = re.compile(r"\$\{?([A-Za-z_]\w*)")
# A command that reaches the cluster through the current kubeconfig.
CLUSTER = re.compile(r"(?:^|[|;&(]|\$\()\s*(kubectl|helm|helmfile)\s|companion_ownership\.py|deployer_access\.py check|"
                     r"live_drift\.py|helmfile_deploy\.py diff|apply-ownership-patches\.sh")
# What a block of this runbook must never run. The read-only plan identity and the separate apply
# identity are retired; the deployer role cannot delete a collection of Secrets; a companion is
# never uninstalled, because that deletes the objects it adopted; claims and namespaces are
# preserved; foreground or background cascading deletes a StatefulSet's Pod; a forced rollback
# or replacement deletes and recreates objects.
FORBIDDEN = (
    (re.compile(r"\bPLAN_(KUBECONFIG|CONTEXT)\b"), "uses the retired read-only plan identity"),
    (re.compile(r"\bAPPLY_(KUBECONFIG|CONTEXT)\b"), "uses the retired separate apply identity"),
    (re.compile(r"\bhelm\s+(\S+\s+)*(uninstall|delete|del|un)\b"), "uninstalls a Helm release"),
    (re.compile(r"\bhelmfile\s+(\S+\s+)*(destroy|delete)\b"), "deletes a Helmfile release"),
    (re.compile(r"\bdelete\s+(--?\S+\s+([^-\s]\S*\s+)?)*(persistentvolumeclaims?|pvc|namespaces?|ns)(\b|/)"),
     "deletes a PersistentVolumeClaim or a namespace"),
    (re.compile(r"--cascade[=\s]+(foreground|background)\b"), "deletes with foreground or background cascading"),
    (re.compile(r"\bhelm\s+(\S+\s+)*rollback\b.*\s--force"), "forces a Helm rollback"),
    (re.compile(r"\breplace\b.*\s--force\b"), "replaces an object by force"),
)
SECRET_DELETE = re.compile(r"\bdelete\b.*\bsecrets?\b|\bsecrets?\b.*\bdelete\b")
SELECTOR = re.compile(r"(--selector\b|\s-l(\s|=|$)|\s-l\S|--all\b|--field-selector\b)")

Block = namedtuple("Block", "marker key code line")


def section(text, heading=SECTION):
    """The lines of the runbook section, from its heading to the next level-two heading."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.rstrip() == heading]
    if len(starts) != 1:
        raise ValueError(f"Expected one {heading!r} heading, found {len(starts)}")
    start = starts[0]
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return start, lines[start:end]


def blocks(text, heading=SECTION):
    """Every fenced block of the section as Block(marker, key, code, line).

    marker is "rehearse", "not-rehearsed", "session" or None when the line before the fence is no
    marker; key is the identifier or the reason; code is the block with its fence's indentation
    removed; line is the 1-based line of the opening fence. Non-bash blocks have language in key
    and marker "other".
    """
    offset, lines = section(text, heading)
    found, i = [], 0
    while i < len(lines):
        opening = FENCE.match(lines[i])
        if not opening:
            i += 1
            continue
        indent, language = opening.group(1), opening.group(2)
        end = next((j for j in range(i + 1, len(lines)) if lines[j].strip() == "```"), None)
        if end is None:
            raise ValueError(f"Unclosed code block at line {offset + i + 1}")
        body = [line[len(indent):] if line.startswith(indent) else line.lstrip() for line in lines[i + 1:end]]
        code = "\n".join(body) + "\n"
        if language != "bash":
            found.append(Block("other", language, code, offset + i + 1))
        else:
            marker = MARKER.match(lines[i - 1]) if i else None
            found.append(Block(marker.group(1) if marker else None, marker.group(2) if marker else None,
                               code, offset + i + 1))
        i = end + 1
    return found


def logical_lines(code):
    """The block's commands, with backslash continuations joined and comments dropped."""
    joined = re.sub(r"\\\n\s*", " ", code)
    return [line.split(" #", 1)[0] for line in joined.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def strict_body(code):
    """The script inside a `bash -euo pipefail <<'EOF'` block, or None when the block has another form."""
    lines = code.rstrip("\n").split("\n")
    if len(lines) < 3 or lines[0] != STRICT_OPEN or lines[-1] != STRICT_CLOSE or STRICT_CLOSE in lines[1:-1]:
        return None
    return lines[1:-1]


def unguarded(body):
    """Documented variables the script reads without a `: "${NAME:?}"` guard in its first lines."""
    guards, count = [], 0
    for line in body:
        if not line.startswith(': "'):
            break
        guards += GUARD.findall(line)
        count += 1
    rest = body[count:]
    used = {name for line in rest for name in REFERENCE.findall(line) if name in VARIABLES}
    # kubectl, Helm and the repository's cluster tools read the deployer kubeconfig unless a line
    # names the administrator's.
    if any(CLUSTER.search(line.strip()) and "ADMIN_KUBECONFIG" not in line for line in logical_lines("\n".join(rest))):
        used.add("KUBECONFIG")
    return sorted(used - set(guards))


def forbidden(code):
    """What a block does that the runbook must never do."""
    problems = []
    for line in logical_lines(code):
        for pattern, why in FORBIDDEN:
            if pattern.search(line):
                problems.append(why)
        if SECRET_DELETE.search(line) and SELECTOR.search(line):
            problems.append("deletes Secrets by selector instead of by name")
    return problems


def form(block):
    """What is wrong with the shape of a block for its marker."""
    body = strict_body(block.code)
    if block.marker == "session":
        found = ["is a session block but runs a strict script"] if body is not None else []
        for line in logical_lines(block.code):
            if (CLUSTER.search(line.strip()) and "config current-context" not in line) or re.search(r"\bgh\s", line):
                found.append("is a session block but runs a cluster or GitHub command")
        return found
    if body is None:
        if block.marker == "rehearse":
            return [f"is not a single {STRICT_OPEN} command"]
        if len(logical_lines(block.code)) != 1:
            return [f"has more than one command and is not a single {STRICT_OPEN} command"]
        return []
    missing = unguarded(body)
    return [f"reads {', '.join(missing)} without a guard in its first lines"] if missing else []


def problems(text, steps, heading=SECTION):
    """Every way the section and the rehearsal's step list disagree with the rules above."""
    found = []
    ids = []
    _, lines = section(text, heading)
    for number, line in enumerate(lines):
        marker = MARKER.match(line)
        if marker and not (number + 1 < len(lines) and FENCE.match(lines[number + 1])
                           and FENCE.match(lines[number + 1]).group(2) == "bash"):
            found.append(f"{marker.group(0).strip()} is not directly followed by a bash block")
    for block in blocks(text, heading):
        where = f"line {block.line}"
        if block.marker == "other":
            continue
        if block.marker is None:
            found.append(f"The bash block at {where} has no rehearse, not-rehearsed or session marker")
            continue
        if block.marker == "rehearse":
            if not IDENTIFIER.fullmatch(block.key):
                found.append(f"The rehearse marker at {where} has an invalid id {block.key!r}")
            ids.append(block.key)
        elif not block.key.strip():
            found.append(f"The {block.marker} marker at {where} gives no reason")
        found += [f"The bash block at {where} {why}" for why in form(block) + forbidden(block.code)]
    for key in sorted({key for key in ids if ids.count(key) > 1}):
        found.append(f"The rehearse id {key} marks more than one block")
    for key in sorted(set(ids) - set(steps)):
        found.append(f"The rehearse id {key} is missing from the rehearsal's step list")
    for key in sorted(set(steps) - set(ids)):
        found.append(f"The rehearsal's step {key} marks no block of the runbook")
    if len(set(steps)) != len(steps):
        found.append("The rehearsal's step list names a block twice")
    return found


def rehearsed(text=None, heading=SECTION):
    """{id: code} of the blocks the rehearsal runs, read from the runbook now."""
    text = DOC.read_text() if text is None else text
    result = {}
    for block in blocks(text, heading):
        if block.marker == "rehearse":
            if block.key in result:
                raise ValueError(f"The rehearse id {block.key} marks more than one block")
            result[block.key] = block.code
    return result
