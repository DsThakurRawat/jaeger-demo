# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class FrozenRenderTests(unittest.TestCase):
    def test_actual_helm_checks_ordinary_objects_and_hooks_before_returning_a_manifest(self):
        base = Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            chart = root / "chart"
            templates = chart / "templates"
            templates.mkdir(parents=True)
            (chart / "Chart.yaml").write_text("apiVersion: v2\nname: frozen-test\nversion: 0.1.0\n")
            (templates / "resources.yaml").write_text("""apiVersion: v1
kind: Secret
metadata:
  name: fixture
stringData:
  sample: {{ .Values.sample | quote }}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: fixture-hook
  annotations:
    helm.sh/hook: pre-upgrade
data:
  marker: {{ .Values.marker | quote }}
""")
            approved = root / "approved.yaml"
            env = dict(os.environ, KUBECONFIG=os.devnull, HELM_CACHE_HOME=str(root / "cache"),
                       HELM_CONFIG_HOME=str(root / "config"), HELM_DATA_HOME=str(root / "data"),
                       HELM_PLUGINS=str(base / "helm-plugins"), JAEGER_APPROVED_MANIFEST=str(approved))
            command = ["helm", "template", "test", str(chart), "--set", "sample=public-fixture,marker=one"]
            approved.write_bytes(subprocess.check_output(command, env=env))
            for args, passes in (([], True), (["--set", "sample=changed-fixture-secret"], False),
                                 (["--set", "marker=two"], False)):
                result = subprocess.run(command + ["--post-renderer", "verify-frozen-manifests", *args],
                                        env=env, capture_output=True)
                self.assertEqual(passes, result.returncode == 0, result.stderr)
                if not passes:
                    self.assertNotIn(b"changed-fixture-secret", result.stdout + result.stderr)
                    self.assertNotIn(b"stringData:", result.stdout + result.stderr)
            approved.write_text("invalid: [")
            bad = subprocess.run(command + ["--post-renderer", "verify-frozen-manifests"], env=env, capture_output=True)
            self.assertNotEqual(bad.returncode, 0)
            self.assertNotIn(b"invalid: [", bad.stderr)
