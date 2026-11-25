# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import ast
from pathlib import Path
import unittest

import runbook_blocks

REHEARSAL = Path(__file__).resolve().parent / "rehearsals/helmfile-runbook/exercise.py"


def rehearsal_steps():
    """The rehearsal's BLOCKS literal, read without importing the rehearsal."""
    tree = ast.parse(REHEARSAL.read_text())
    value, = [node.value for node in tree.body if isinstance(node, ast.Assign)
              and [target.id for target in node.targets if isinstance(target, ast.Name)] == ["BLOCKS"]]
    return ast.literal_eval(value)


def document(*blocks, heading=runbook_blocks.SECTION):
    """A runbook with the given (marker line or None, code) bash blocks, between two other sections."""
    parts = ["# Runbook", "", "## Before", "", "```bash", "echo outside the section", "```", "", heading, ""]
    for marker, code in blocks:
        parts += ["Some text.", ""]
        if marker is not None:
            parts.append(marker)
        parts += ["```bash", code, "```", ""]
    parts += ["## After", "", "```bash", "echo outside again", "```", ""]
    return "\n".join(parts)


class RunbookBlocksTest(unittest.TestCase):
    def test_the_runbook_and_the_rehearsal_agree(self):
        text = runbook_blocks.DOC.read_text()
        steps = rehearsal_steps()
        self.assertEqual([], runbook_blocks.problems(text, steps))
        self.assertEqual(set(steps), set(runbook_blocks.rehearsed(text)))
        # The section has blocks of both kinds, and every not-rehearsed one says why.
        markers = [block.marker for block in runbook_blocks.blocks(text) if block.marker != "other"]
        self.assertIn("not-rehearsed", markers)
        self.assertGreaterEqual(markers.count("rehearse"), 20)

    def test_the_rehearsed_blocks_never_use_a_retired_identity_or_a_destructive_shortcut(self):
        for key, code in runbook_blocks.rehearsed().items():
            with self.subTest(block=key):
                for retired in ("PLAN_KUBECONFIG", "PLAN_CONTEXT", "APPLY_KUBECONFIG", "APPLY_CONTEXT", "helm uninstall"):
                    self.assertNotIn(retired, code)
                self.assertEqual([], runbook_blocks.forbidden(code))

    def test_the_whole_runbook_names_no_retired_identity(self):
        section = "\n".join(runbook_blocks.section(runbook_blocks.DOC.read_text())[1])
        for retired in ("PLAN_KUBECONFIG", "PLAN_CONTEXT", "APPLY_KUBECONFIG", "APPLY_CONTEXT", "Under revision"):
            self.assertNotIn(retired, section)

    def test_an_unmarked_bash_block_fails(self):
        text = document(("<!-- rehearse: one -->", "echo one"), (None, "echo two"))
        found = runbook_blocks.problems(text, ["one"])
        self.assertEqual(1, len(found), found)
        self.assertIn("has no rehearse or not-rehearsed marker", found[0])

    def test_a_duplicated_or_unlisted_id_fails(self):
        text = document(("<!-- rehearse: one -->", "echo a"), ("<!-- rehearse: one -->", "echo b"),
                        ("<!-- rehearse: two -->", "echo c"))
        found = runbook_blocks.problems(text, ["one"])
        self.assertIn("The rehearse id one marks more than one block", found)
        self.assertIn("The rehearse id two is missing from the rehearsal's step list", found)
        with self.assertRaisesRegex(ValueError, "more than one block"):
            runbook_blocks.rehearsed(text)

    def test_a_step_without_a_block_fails(self):
        text = document(("<!-- rehearse: one -->", "echo a"))
        self.assertEqual(["The rehearsal's step gone marks no block of the runbook"],
                         runbook_blocks.problems(text, ["one", "gone"]))
        self.assertIn("The rehearsal's step list names a block twice", runbook_blocks.problems(text, ["one", "one"]))

    def test_a_marker_needs_a_bash_block_right_after_it_and_a_reason(self):
        text = document(("<!-- not-rehearsed:  -->", "echo a"))
        self.assertIn("gives no reason", " ".join(runbook_blocks.problems(text, [])))
        stray = document().replace("## After", "<!-- rehearse: stray -->\n\nText.\n\n## After")
        self.assertIn("is not directly followed by a bash block", " ".join(runbook_blocks.problems(stray, [])))
        self.assertIn("invalid id", " ".join(runbook_blocks.problems(document(("<!-- rehearse: Bad_Id -->", "true")),
                                                                    ["Bad_Id"])))

    def test_blocks_outside_the_section_and_other_languages_are_not_checked(self):
        text = document(("<!-- rehearse: one -->", "echo a")).replace("## After", "```text\nnot code\n```\n\n## After")
        self.assertEqual([], runbook_blocks.problems(text, ["one"]))
        self.assertEqual({"one": "echo a\n"}, runbook_blocks.rehearsed(text))

    def test_indented_blocks_in_a_list_item_are_read_without_their_indentation(self):
        text = document().replace(
            "## After", "1. A step:\n\n   <!-- rehearse: listed -->\n   ```bash\n   if true; then\n     echo in\n   fi\n   ```\n\n## After")
        self.assertEqual({"listed": "if true; then\n  echo in\nfi\n"}, runbook_blocks.rehearsed(text))

    def test_forbidden_commands_fail_in_any_block_of_the_section(self):
        cases = {
            'helm --namespace "$ns" uninstall demo-resources': "uninstalls a Helm release",
            "helm uninstall demo-resources -n jaeger": "uninstalls a Helm release",
            "helmfile -f helmfile-resources.yaml.gotmpl destroy": "deletes a Helmfile release",
            'kubectl --kubeconfig "$PLAN_KUBECONFIG" get pods': "retired read-only plan identity",
            'companion_ownership.py --context "$PLAN_CONTEXT"': "retired read-only plan identity",
            'kubectl --kubeconfig "$APPLY_KUBECONFIG" get pods': "retired separate apply identity",
            "kubectl -n jaeger delete secret --selector owner=helm,name=demo-resources": "by selector",
            "kubectl -n jaeger delete secrets -l owner=helm": "by selector",
            'kubectl --namespace "$ns" \\\n  delete secret --selector owner=helm': "by selector",
            "kubectl -n jaeger delete secret --all": "by selector",
        }
        for code, why in cases.items():
            for marker in ("<!-- rehearse: one -->", "<!-- not-rehearsed: needs GitHub -->"):
                with self.subTest(code=code, marker=marker):
                    steps = ["one"] if "rehearse:" in marker else []
                    found = runbook_blocks.problems(document((marker, code)), steps)
                    self.assertTrue(any(why in problem for problem in found), found)

    def test_named_deletes_and_selector_reads_are_allowed(self):
        allowed = [
            'kubectl --namespace "$ns" delete secret "$secret"',
            "kubectl --namespace \"$ns\" get secret --selector owner=helm,name=demo-resources \\\n  --output name",
            "helm history demo-resources --namespace \"$ns\" --max 1 2>&1 | grep 'release: not found'",
            'helm rollback "$release" "$REVISION" --namespace "$ns" --wait=watcher',
            "kubectl --namespace opensearch delete statefulset opensearch-cluster-single --cascade=orphan",
        ]
        for code in allowed:
            with self.subTest(code=code):
                self.assertEqual([], runbook_blocks.forbidden(code))

    def test_the_section_must_exist_once(self):
        with self.assertRaisesRegex(ValueError, "Expected one"):
            runbook_blocks.blocks("# No runbook here\n")
        with self.assertRaisesRegex(ValueError, "Unclosed code block"):
            runbook_blocks.blocks(runbook_blocks.SECTION + "\n\n<!-- rehearse: a -->\n```bash\necho\n")


if __name__ == "__main__":
    unittest.main()
