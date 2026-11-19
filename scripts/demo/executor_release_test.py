# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
RELEASE = yaml.safe_load((ROOT / ".github/workflows/release-executor.yml").read_text())["jobs"]
CI = yaml.safe_load((ROOT / ".github/workflows/executor-ci.yml").read_text())["jobs"]
DIGEST = "sha256:" + hashlib.sha256(b"manifest").hexdigest()

# Stand in for the two endpoints. The real workflow shells make all digest
# comparisons, decide whether to release a receipt and clean up login state.
SKOPEO = """#!/bin/bash
set -eu
cmd=$1
shift
case "$cmd" in
  inspect)
    case "$*" in
      *oci-archive:*) cat "$RUNNER_TEMP/executor.tar" ;;
      *docker://*) printf %s "$REGISTRY_MANIFEST" ;;
      *) exit 1 ;;
    esac ;;
  manifest-digest)
    printf 'sha256:%s' "$(shasum -a 256 "$1" | cut -d' ' -f1)" ;;
  login)
    test "$1" = "--authfile" && test "$4" = "fixture"
    cat > "$2" ;;
  copy)
    test "$1" = "--preserve-digests" && test "$2" = "--dest-authfile"
    test "$(cat "$3")" = "fixture-credential" && test "$4" = "--digestfile"
    test "$6" = "oci-archive:$RUNNER_TEMP/executor.tar"
    test "$7" = "docker://$PACKAGE:$GITHUB_SHA"
    printf %s "$COPY_DIGEST" > "$5" ;;
  *) exit 1 ;;
esac
"""


class ReleaseTests(unittest.TestCase):
    def test_release_rehearses_before_exporting_only_the_image_archive(self):
        candidate = RELEASE["candidate"]
        self.assertEqual(candidate["permissions"], {"contents": "read"})
        steps = candidate["steps"]
        self.assertEqual(steps[-2]["uses"], CI["rehearsal"]["steps"][-1]["uses"])
        self.assertEqual(steps[-1]["with"]["path"], "${{ runner.temp }}/executor.tar")
        self.assertEqual(candidate["outputs"]["digest"], "${{ steps.rehearsal.outputs.digest }}")
        publish = RELEASE["publish"]
        self.assertEqual(publish["needs"], "candidate")
        self.assertEqual(publish["permissions"], {"packages": "write"})
        self.assertEqual(publish["steps"][0]["with"]["artifact-ids"], "${{ needs.candidate.outputs.artifact_id }}")
        self.assertFalse(any("checkout" in step.get("uses", "") for step in publish["steps"]))

    def test_receipt_requires_matching_archive_copy_and_registry_manifests(self):
        verify, publish = [step["run"] for step in RELEASE["publish"]["steps"] if "run" in step]
        for archive, copied, registry, expected in [
            ("manifest", DIGEST, "manifest", True),
            ("changed", DIGEST, "manifest", False),
            ("manifest", "sha256:" + "b" * 64, "manifest", False),
            ("manifest", DIGEST, "changed", False),
        ]:
            with self.subTest(archive=archive, copied=copied, registry=registry), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                (root / "executor.tar").write_text(archive)
                (root / "skopeo").write_text(SKOPEO)
                (root / "skopeo").chmod(0o700)
                summary = root / "summary"
                env = dict(PATH=tmp + os.pathsep + os.environ["PATH"], RUNNER_TEMP=tmp,
                           PACKAGE="ghcr.io/jaegertracing/jaeger-demo-executor", GITHUB_ACTOR="fixture",
                           GITHUB_SHA="a" * 40, GITHUB_STEP_SUMMARY=str(summary), EXPECTED_DIGEST=DIGEST,
                           COPY_DIGEST=copied, REGISTRY_MANIFEST=registry)
                checked = subprocess.run(["bash", "-c", verify], env=env, check=False)
                self.assertEqual(checked.returncode == 0, archive == "manifest")
                if checked.returncode == 0:
                    result = subprocess.run(["bash", "-c", publish], env=dict(env, PACKAGE_TOKEN="fixture-credential"),
                                            capture_output=True, check=False)
                    self.assertNotIn(b"fixture-credential", result.stdout + result.stderr)
                    self.assertEqual(result.returncode == 0, expected)
                self.assertEqual(summary.exists(), expected)
                self.assertEqual(list(root.glob("registry.*")), [])
                if expected:
                    self.assertIn(f"{env['PACKAGE']}@{DIGEST}", summary.read_text())
