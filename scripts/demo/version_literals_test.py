# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Every Helm and Kubernetes version literal must agree with the toolchain lock.

The source of truth is scripts/demo/helmfile-tools.lock.json (read here, never edited by this
test). Bumping either version in a Renovate PR or by hand fails this test with every straggler
named. Rehearsal receipts, README records of past runs and the envtest pin are history or
unrelated and are deliberately not checked.
"""

import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest

import yaml

import deploy

LOCK = "scripts/demo/helmfile-tools.lock.json"
INVENTORY = "ops/rbac/chart-objects.json"
VER = r"v?\d+\.\d+\.\d+"

# (path, regex with one capture group, "helm" | "kube", minimum number of matches)
LITERALS = (
    ("scripts/demo/oci_test.py", rf'"--kube-version", "({VER})"', "kube", 1),
    ("scripts/demo/rehearsals/helmfile-applications/equivalence.py", rf'"--kube-version", "({VER})"', "kube", 1),
    (".github/workflows/ci.yml", rf"kindest/node:({VER})@", "kube", 1),
    # The deployer-access job's kubectl. Its SHA-256 in ci.yml changes with the version.
    (".github/workflows/ci.yml", rf"dl\.k8s\.io/release/({VER})/bin/linux/amd64/kubectl", "kube", 1),
    ("examples/oci/monitoring-values.yaml", r'depName=registry\.k8s\.io/kubectl\s+tag: "(v\d+\.\d+\.\d+)@sha256', "kube", 1),
)
WORKFLOWS = (".github/workflows/ci.yml", ".github/workflows/ci-deploy-demo.yml")


def bare(version):
    return version.removeprefix("v")


def next_patch(version):
    """A deliberately different version derived from a source-of-truth literal."""
    major, minor, patch = bare(version).split(".")
    return f"{'v' if version.startswith('v') else ''}{major}.{minor}.{int(patch) + 1}"


def source_versions(root):
    lock = json.loads((root / LOCK).read_text())
    return lock["tools"]["helm"]["version"], lock["kubernetesSchema"]["version"]


def setup_helm_versions(text):
    versions = []
    for job in (yaml.safe_load(text).get("jobs") or {}).values():
        for step in job.get("steps") or []:
            if str(step.get("uses", "")).startswith("azure/setup-helm"):
                versions.append(str((step.get("with") or {}).get("version")))
    return versions


def stragglers(root):
    helm, kube = source_versions(root)
    want = {"helm": helm, "kube": kube}
    bad = []

    def compare(path, kind, got):
        if bare(got) != bare(want[kind]):
            bad.append(f"{path}: {kind} {got}, expected {bare(want[kind])}")

    for path, pattern, kind, minimum in LITERALS:
        found = re.findall(pattern, (root / path).read_text())
        if len(found) < minimum:
            bad.append(f"{path}: expected at least {minimum} {kind} match(es) for {pattern!r}, found {len(found)}")
        for got in found:
            compare(path, kind, got)
    matrix = json.loads((root / INVENTORY).read_text())
    compare(f"{INVENTORY} (helm)", "helm", str(matrix.get("helm")))
    compare(f"{INVENTORY} (targetKubernetes)", "kube", str(matrix.get("targetKubernetes")))
    for path in WORKFLOWS:
        found = setup_helm_versions((root / path).read_text())
        if not found:
            bad.append(f"{path}: no azure/setup-helm step with a version input")
        for got in found:
            compare(path, "helm", got)
    return bad


class VersionLiteralTests(unittest.TestCase):
    def test_every_helm_and_kubernetes_literal_matches_the_toolchain_lock(self):
        helm, kube = source_versions(deploy.ROOT)
        bad = stragglers(deploy.ROOT)
        self.assertEqual(bad, [], f"Helm {helm} and Kubernetes {kube} come from {LOCK}; update every straggler together:\n"
                         + "\n".join(bad))

    def test_a_stale_literal_is_reported_with_its_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for path in {LOCK, INVENTORY, *WORKFLOWS, *(entry[0] for entry in LITERALS)}:
                (root / path).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(deploy.ROOT / path, root / path)
            self.assertEqual(stragglers(root), [])
            helm, kube = source_versions(root)
            stale_helm, stale_kube = next_patch(helm), next_patch(kube)
            workflow = root / WORKFLOWS[0]
            text = workflow.read_text()
            self.assertIn(f"version: {helm}", text)
            workflow.write_text(text.replace(f"version: {helm}", f"version: {stale_helm}"))
            fixture = root / LITERALS[0][0]
            text = fixture.read_text()
            literal = f'"--kube-version", "{bare(kube)}"'
            self.assertIn(literal, text)
            fixture.write_text(text.replace(literal, f'"--kube-version", "{bare(stale_kube)}"'))
            bad = stragglers(root)
            self.assertEqual(len(bad), 2, bad)
            self.assertTrue(any(line.startswith(WORKFLOWS[0]) and stale_helm in line for line in bad))
            self.assertTrue(any(line.startswith(LITERALS[0][0]) and bare(stale_kube) in line for line in bad))


if __name__ == "__main__":
    unittest.main()
