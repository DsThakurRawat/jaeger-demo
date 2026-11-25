# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import base64
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

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

    def test_summary_names_which_side_was_verified(self):
        empty = records()
        verified = diff.summary(empty, empty, "a" * 40, "b" * 40, False, base_verified=True)
        self.assertIn("Head charts: verified against the chart archive lock. Base charts: verified against the lock.", verified)
        unverified = diff.summary(empty, empty, "a" * 40, "b" * 40, False)
        self.assertIn("Base charts: NOT verified, rendered from the chart publishers.", unverified)

    def test_a_checkout_without_the_state_command_is_told_apart_from_a_failing_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "scripts/demo/chart_archives.py"
            self.assertFalse(diff.has_state_command(root))
            script.parent.mkdir(parents=True)
            script.write_text('import sys; print("chart_archives.py pull <chart>", file=sys.stderr); sys.exit(2)')
            self.assertFalse(diff.has_state_command(root))
            script.write_text('import sys; print("chart_archives.py state <dir>", file=sys.stderr); sys.exit(2)')
            self.assertTrue(diff.has_state_command(root))
            script.write_text('import sys; print("chart_archives.py state <dir>", file=sys.stderr); sys.exit(1)')
            self.assertFalse(diff.has_state_command(root))

    def test_a_script_that_defines_state_but_is_not_recognized_fails_instead_of_downgrading(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "scripts/demo/chart_archives.py"
            script.parent.mkdir(parents=True)
            script.write_text('import sys\ndef locked_state(root, dest):\n    pass\nprint("usage changed", file=sys.stderr)\nsys.exit(2)\n')
            with self.assertRaises(diff.VerificationError):
                diff.has_state_command(root)

    def test_the_real_script_of_this_checkout_is_recognized(self):
        self.assertTrue(diff.has_state_command(Path(__file__).resolve().parents[2]))

    def test_a_state_failure_is_a_verification_error_without_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "scripts/demo/chart_archives.py"
            script.parent.mkdir(parents=True)
            script.write_text('import sys; print("private-payload"); sys.exit("private-payload")')
            with self.assertRaises(diff.VerificationError) as raised:
                diff.locked_state(root, root / "temp", "archives")
            self.assertNotIn("private-payload", str(raised.exception))

    def run_main(self, *, legacy, base_has_state, state_error=None):
        calls = []
        def fake_state(root, temp, name):
            calls.append(("state", root.name))
            if state_error == root.name:
                raise diff.VerificationError("bad")
            return ".helmfile-locked.yaml"
        def fake_render(root, temp, legacy_flag, **kwargs):
            calls.append(("render", root.name, kwargs.get("state")))
            return {}
        with tempfile.TemporaryDirectory() as tmp:
            base, head = Path(tmp) / "base", Path(tmp) / "head"
            for path in (base, head):
                path.mkdir()
            (head / "helmfile.yaml").write_text("")
            if not legacy:
                (base / "helmfile.yaml").write_text("")
            with mock.patch.object(diff, "locked_state", fake_state), \
                 mock.patch.object(diff, "has_state_command", lambda root: base_has_state), \
                 mock.patch.object(diff, "render", fake_render), \
                 mock.patch.object(diff, "revision", lambda root: "a" * 40), \
                 mock.patch.object(sys, "argv", ["pr_render_diff.py", "--base", str(base), "--head", str(head)]), \
                 mock.patch("sys.stdout") as out:
                try:
                    diff.main()
                    code = 0
                except SystemExit as exit_:
                    code = exit_.code
                text = "".join(call.args[0] for call in out.write.call_args_list)
        return code, calls, text

    def test_head_and_a_capable_base_both_render_from_locked_state(self):
        code, calls, text = self.run_main(legacy=False, base_has_state=True)
        self.assertEqual(code, 0)
        self.assertEqual([c for c in calls if c[0] == "render"],
                         [("render", "base", ".helmfile-locked.yaml"), ("render", "head", ".helmfile-locked.yaml")])
        self.assertIn("Base charts: verified against the lock.", text)

    def test_a_base_without_the_state_command_keeps_the_publisher_render_and_says_so(self):
        code, calls, text = self.run_main(legacy=False, base_has_state=False)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("state", "head"), ("render", "base", "helmfile.yaml"), ("render", "head", ".helmfile-locked.yaml")])
        self.assertIn("Base charts: NOT verified", text)

    def test_a_script_era_base_is_labeled_unverified(self):
        code, calls, text = self.run_main(legacy=True, base_has_state=True)
        self.assertEqual(code, 0)
        self.assertNotIn(("state", "base"), calls)
        self.assertIn("Base charts: NOT verified", text)

    def test_a_head_that_does_not_verify_fails_before_any_render(self):
        code, calls, _ = self.run_main(legacy=False, base_has_state=False, state_error="head")
        self.assertNotEqual(code, 0)
        self.assertEqual([c for c in calls if c[0] == "render"], [])

    def test_a_capable_base_that_does_not_verify_fails_instead_of_falling_back(self):
        code, calls, _ = self.run_main(legacy=False, base_has_state=True, state_error="base")
        self.assertNotEqual(code, 0)
        self.assertEqual([c for c in calls if c[0] == "render"], [])


if __name__ == "__main__":
    unittest.main()
