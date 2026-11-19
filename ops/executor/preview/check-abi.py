# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Verify the installed offline Helm preview tool using two harmless JSON fixtures."""

import json
import subprocess
import sys


def check(binary, version, kind, mode):
    original = {
        "apiVersion": version,
        "kind": kind,
        "metadata": {"name": "sample", "labels": {"fixture": "old"}},
    }
    desired = {**original, "metadata": {"name": "sample", "labels": {"fixture": "new"}}}
    request = json.dumps({"original": original, "desired": desired, "live": original}).encode()
    output = subprocess.run([binary], input=request, capture_output=True, check=True, timeout=10)
    if json.loads(output.stdout) != {"type": mode, "patch": {"metadata": {"labels": {"fixture": "new"}}}}:
        raise ValueError("The installed Helm preview tool returned an incompatible patch")


if __name__ == "__main__":
    check(sys.argv[1], "v1", "ConfigMap", "strategic")
    check(sys.argv[1], "fixtures.invalid/v1", "Example", "merge")
