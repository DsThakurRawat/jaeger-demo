#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Print the application releases and prerequisites selected in one demo stack."""

import argparse
from graphlib import TopologicalSorter
from pathlib import Path

import yaml


STATE = Path(__file__).resolve().parents[2] / "helmfile.yaml"


def selection(stack, component, releases):
    dependencies = {}
    selected = set()
    for release in releases:
        if release["labels"]["stack"] != stack:
            continue
        name = release["namespace"] + "/" + release["name"]
        if name in dependencies:
            raise ValueError(f"Duplicate release {name}")
        dependencies[name] = release.get("needs", [])
        if component == "all" or component == release["labels"]["component"]:
            selected.add(name)
    if not selected:
        raise ValueError(f"No {component} release in stack {stack}")

    order = list(TopologicalSorter(dependencies).static_order())
    for name in reversed(order):
        if name not in dependencies:
            raise ValueError(f"Unknown or cross-stack prerequisite {name}")
        if name in selected:
            selected.update(dependencies[name])
    return [name for name in order if name in selected]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", choices=("oci", "otel"), required=True)
    parser.add_argument("--component", default="all")
    args = parser.parse_args()
    releases = yaml.safe_load(STATE.read_text())["releases"]
    try:
        print("\n".join(selection(args.stack, args.component, releases)))
    except ValueError as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
