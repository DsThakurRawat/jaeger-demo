# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import base64
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import pr_render_diff as diff


def records(*resources):
    data = {}
    diff.index_objects(data, "default", "demo-resources", resources)
    return data


class RenderDiffTest(unittest.TestCase):
    def test_changes_report_only_resource_identities(self):
        def resource(kind, name, value):
            return {"apiVersion": "v1", "kind": kind, "metadata": {"name": name, "namespace": "default"},
                    "data": {"credential": base64.b64encode(value.encode()).decode()}}
        old = resource("Secret", "protected", "old-private-value")
        new = resource("Secret", "protected", "new-private-value")
        added = resource("ConfigMap", "created", "private-annotation")
        removed = resource("Secret", "retired", "old-private-value")
        output = diff.summary(records(old, removed), records(new, added), "a" * 40, "b" * 40, True)
        self.assertIn("1 added, 1 modified, 1 removed", output)
        for action, obj in (("Modified", new), ("Added", added), ("Removed", removed)):
            self.assertIn(f"| {action} | `default/demo-resources` | `v1` | `{obj['kind']}` | `default` | `{obj['metadata']['name']}`", output)
            self.assertNotIn(obj["data"]["credential"], output)
        self.assertNotIn("private", output)
        self.assertIn("script-era", output)

    def test_reordered_manifest_keys_and_documents_are_a_noop(self):
        left = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "names"}, "data": {"a": "b", "b": "a"}}
        right = dict(reversed(left.items()))
        right["data"] = dict(reversed(right["data"].items()))
        other = dict(left, metadata={"name": "other"})
        output = diff.summary(records(left, other), records(other, right), "a" * 40, "b" * 40, False)
        self.assertIn("0 added, 0 modified, 0 removed", output)
        self.assertNotIn("script-era", output)
        self.assertNotIn("Change | Release", output)

    def test_dirty_and_untracked_render_inputs_cannot_claim_a_commit_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(["git", "-C", tmp, *args], stderr=subprocess.DEVNULL)
            git("init", "-q")
            git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-qm", "Fixture")
            self.assertEqual(diff.revision(root), git("rev-parse", "HEAD").decode().strip())
            (root / "extra.yaml").write_text("unreviewed")
            with self.assertRaisesRegex(ValueError, "clean"):
                diff.revision(root)

    def test_failed_child_diagnostics_stay_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError) as raised:
                diff.child(Path(tmp), {}, sys.executable, "-c",
                           'import sys; print("private-payload"); sys.exit("private-payload")')
        self.assertNotIn("private-payload", str(raised.exception))

    def test_duplicate_and_unsafe_names_cannot_enter_summary(self):
        obj = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "ok"}}
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            records(obj, obj)
        for invalid in ("new\n## fake", "name|", "left`right", "<img>"):
            with self.assertRaisesRegex(ValueError, "Invalid Kubernetes identity"):
                records(dict(obj, metadata={"name": invalid}))


if __name__ == "__main__":
    unittest.main()
