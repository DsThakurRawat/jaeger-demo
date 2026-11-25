# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Read the bash blocks of the Helmfile adoption runbook in docs/cutover.md.

Each bash block of the section carries a marker on the line before its opening fence:
`<!-- rehearse: <id> -->` when the kind rehearsal (scripts/demo/rehearsals/helmfile-runbook)
runs it verbatim, or `<!-- not-rehearsed: <reason> -->` when kind cannot run it. The rehearsal
reads the blocks from the document at run time, so it holds no copy of them.
"""

from collections import namedtuple
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs/cutover.md"
SECTION = "## Helmfile adoption"
FENCE = re.compile(r"^(\s*)```(\S*)\s*$")
MARKER = re.compile(r"^\s*<!-- (rehearse|not-rehearsed): (.*?) -->\s*$")
IDENTIFIER = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# What a block of this runbook must never run. The read-only plan identity and the separate apply
# identity are retired; the deployer role cannot delete a collection of Secrets; a companion is
# never uninstalled, because that deletes the objects it adopted.
FORBIDDEN = (
    (re.compile(r"\bPLAN_(KUBECONFIG|CONTEXT)\b"), "uses the retired read-only plan identity"),
    (re.compile(r"\bAPPLY_(KUBECONFIG|CONTEXT)\b"), "uses the retired separate apply identity"),
    (re.compile(r"\bhelm\s+(\S+\s+)*(uninstall|delete|del|un)\b"), "uninstalls a Helm release"),
    (re.compile(r"\bhelmfile\s+(\S+\s+)*(destroy|delete)\b"), "deletes a Helmfile release"),
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

    marker is "rehearse", "not-rehearsed" or None when the line before the fence is no marker;
    key is the identifier or the reason; code is the block with its fence's indentation removed;
    line is the 1-based line of the opening fence. Non-bash blocks have language in key and
    marker "other".
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
    return [line.split(" #", 1)[0] for line in joined.splitlines() if not line.lstrip().startswith("#")]


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
            found.append(f"The bash block at {where} has no rehearse or not-rehearsed marker")
            continue
        if block.marker == "rehearse":
            if not IDENTIFIER.fullmatch(block.key):
                found.append(f"The rehearse marker at {where} has an invalid id {block.key!r}")
            ids.append(block.key)
        elif not block.key.strip():
            found.append(f"The not-rehearsed marker at {where} gives no reason")
        found += [f"The bash block at {where} {why}" for why in forbidden(block.code)]
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
