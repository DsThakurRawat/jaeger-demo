# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Mounted in disposable fixture Pods in place of the application adapter."""

from pathlib import Path

import scheduled_bundle as custody

KEYS = Path("/var/run/jaeger-bundle-keys")
BUNDLES = Path("/var/lib/jaeger-bundles")
OUTCOMES = Path("/var/lib/jaeger-outcomes")


def key_names():
    return {p.name for p in KEYS.iterdir() if not p.name.startswith(".")}


def mount_flags(path):
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if fields[4] == str(path.resolve()):
            return fields[5].split(",")
    raise AssertionError("Expected explicit Kubernetes mount")


def frozen(slot, configuration_sha):
    return {p: (p + slot + configuration_sha).encode() for p in custody.MEMBERS}


def prepare(slot, configuration_sha):
    assert key_names() == {"signing-key.pem", "verification-key.pem", "recipient-public.pem"}
    assert "rw" in mount_flags(BUNDLES) and not OUTCOMES.exists()
    assert "ro" in mount_flags(KEYS)
    return frozen(slot, configuration_sha)


def apply(files, *, slot, configuration_sha, assert_locked, execution_deadline):
    assert_locked()
    assert key_names() == {"verification-key.pem", "decryption-key.pem"}
    assert "ro" in mount_flags(BUNDLES) and "rw" in mount_flags(OUTCOMES) and "ro" in mount_flags(KEYS)
    assert frozen(slot, configuration_sha) == files
    assert_locked()
    return Path("/fixture/outcome").read_text().strip() == "success"
