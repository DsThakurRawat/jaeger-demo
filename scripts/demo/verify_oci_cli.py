# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Reject an OCI installation that differs from the reviewed constraints."""

from importlib import metadata
from pathlib import Path
import re
import subprocess


CONSTRAINTS = Path(__file__).with_name('oci-cli-constraints.txt')


def read_constraints(path=CONSTRAINTS):
    pins = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith('#'):
            continue
        match = re.fullmatch(r'([A-Za-z0-9_.-]+)==([^\s=]+)', line)
        if not match:
            raise ValueError('OCI constraints must contain exact package==version pins')
        name = re.sub(r'[-_.]+', '-', match[1]).lower()
        if name in pins:
            raise ValueError('Duplicate OCI dependency pin: ' + name)
        pins[name] = match[2]
    if 'oci-cli' not in pins:
        raise ValueError('Missing exact oci-cli version in constraints')
    return pins


def verify_versions(pins):
    for name, expected in pins.items():
        try:
            actual = metadata.version(name)
        except metadata.PackageNotFoundError as error:
            raise RuntimeError('Missing OCI dependency: ' + name) from error
        if actual != expected:
            raise RuntimeError(f'OCI dependency version mismatch: {name}: expected {expected}, got {actual}')
    actual_cli = subprocess.check_output(['oci', '--version'], text=True).strip()
    if actual_cli != pins['oci-cli']:
        raise RuntimeError(f"OCI CLI version mismatch: expected {pins['oci-cli']}, got {actual_cli}")


def main():
    pins = read_constraints()
    verify_versions(pins)
    print(f"Verified OCI CLI {pins['oci-cli']} and all {len(pins)} pinned distributions")


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from error
