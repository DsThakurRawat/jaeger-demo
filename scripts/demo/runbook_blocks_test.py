# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import ast
from pathlib import Path
import re
import unittest

import yaml

import runbook_blocks

REHEARSAL = Path(__file__).resolve().parent / "rehearsals/helmfile-runbook/exercise.py"
WORKFLOW = runbook_blocks.ROOT / ".github/workflows/ci-deploy-demo.yml"


def rehearsal_steps():
    """The rehearsal's BLOCKS literal, read without importing the rehearsal."""
    tree = ast.parse(REHEARSAL.read_text())
    value, = [node.value for node in tree.body if isinstance(node, ast.Assign)
              and [target.id for target in node.targets if isinstance(target, ast.Name)] == ["BLOCKS"]]
    return ast.literal_eval(value)


def strict(*lines):
    """A block in the runbook's strict form."""
    return "\n".join([runbook_blocks.STRICT_OPEN, *lines, runbook_blocks.STRICT_CLOSE])


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


def section_commands(pattern):
    """Logical lines of the runbook's bash blocks that match pattern."""
    return [line for block in runbook_blocks.blocks(runbook_blocks.DOC.read_text()) if block.marker != "other"
            for line in runbook_blocks.logical_lines(block.code) if re.search(pattern, line)]


class RunbookBlocksTest(unittest.TestCase):
    def test_the_runbook_and_the_rehearsal_agree(self):
        text = runbook_blocks.DOC.read_text()
        steps = rehearsal_steps()
        self.assertEqual([], runbook_blocks.problems(text, steps))
        self.assertEqual(set(steps), set(runbook_blocks.rehearsed(text)))
        markers = [block.marker for block in runbook_blocks.blocks(text) if block.marker != "other"]
        self.assertIn("not-rehearsed", markers)
        self.assertIn("session", markers)
        self.assertGreaterEqual(markers.count("rehearse"), 20)

    def test_every_rehearsed_block_is_strict_and_guarded(self):
        for key, code in runbook_blocks.rehearsed().items():
            with self.subTest(block=key):
                body = runbook_blocks.strict_body(code)
                self.assertIsNotNone(body)
                self.assertTrue(body[0].startswith(': "${'), "The first line guards the variables the block reads")
                self.assertEqual([], runbook_blocks.unguarded(body))

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

    def test_dispatches_use_the_workflow_inputs_and_choices(self):
        workflow = yaml.safe_load(WORKFLOW.read_text())
        inputs = workflow[True]["workflow_dispatch"]["inputs"]
        dispatches = section_commands(r"\bgh workflow run\b")
        self.assertEqual(2, len(dispatches), dispatches)
        for line in dispatches:
            with self.subTest(dispatch=line):
                self.assertIn("gh workflow run ci-deploy-demo.yml --repo jaegertracing/jaeger-demo --ref main", line)
                given = dict(re.findall(r"-f (\w+)=(\S+)", line))
                self.assertLessEqual(set(given), set(inputs))
                self.assertEqual("helmfile", given["engine"])
                self.assertEqual('"$REVIEWED_COMMIT"', given["expected_config_sha"])
                for name, value in given.items():
                    if "$" not in value and inputs[name].get("type") == "choice":
                        self.assertIn(value, inputs[name]["options"], name)
        install, = [line for line in dispatches if "install-companion" in line]
        self.assertIn('-f companion_namespace="$ns"', install)

    def test_the_downloaded_artifact_is_the_one_the_workflow_uploads(self):
        steps = yaml.safe_load(WORKFLOW.read_text())["jobs"]["deploy"]["steps"]
        upload, = [step for step in steps if step.get("name") == "Upload the deployment record"]
        name = upload["with"]["name"].replace("${{ github.run_id }}", "$RUN_ID").replace("${{ github.run_attempt }}", "1")
        self.assertEqual("deploy-record.json", upload["with"]["path"])
        downloads = section_commands(r"\bgh run download\b")
        self.assertEqual(1, len(downloads), downloads)
        self.assertIn(f'--name "{name}"', downloads[0])
        self.assertIn('gh run download "$RUN_ID" --repo jaegertracing/jaeger-demo', downloads[0])

    def test_an_unmarked_bash_block_fails(self):
        text = document(("<!-- rehearse: one -->", strict(': "${ns:?}"', 'echo "$ns"')), (None, "echo two"))
        found = runbook_blocks.problems(text, ["one"])
        self.assertEqual(1, len(found), found)
        self.assertIn("has no rehearse, not-rehearsed or session marker", found[0])

    def test_a_duplicated_or_unlisted_id_fails(self):
        text = document(("<!-- rehearse: one -->", strict("echo a")), ("<!-- rehearse: one -->", strict("echo b")),
                        ("<!-- rehearse: two -->", strict("echo c")))
        found = runbook_blocks.problems(text, ["one"])
        self.assertIn("The rehearse id one marks more than one block", found)
        self.assertIn("The rehearse id two is missing from the rehearsal's step list", found)
        with self.assertRaisesRegex(ValueError, "more than one block"):
            runbook_blocks.rehearsed(text)

    def test_a_step_without_a_block_fails(self):
        text = document(("<!-- rehearse: one -->", strict("echo a")))
        self.assertEqual(["The rehearsal's step gone marks no block of the runbook"],
                         runbook_blocks.problems(text, ["one", "gone"]))
        self.assertIn("The rehearsal's step list names a block twice", runbook_blocks.problems(text, ["one", "one"]))

    def test_a_marker_needs_a_bash_block_right_after_it_and_a_reason(self):
        text = document(("<!-- not-rehearsed:  -->", "echo a"))
        self.assertIn("gives no reason", " ".join(runbook_blocks.problems(text, [])))
        stray = document().replace("## After", "<!-- rehearse: stray -->\n\nText.\n\n## After")
        self.assertIn("is not directly followed by a bash block", " ".join(runbook_blocks.problems(stray, [])))
        self.assertIn("invalid id", " ".join(runbook_blocks.problems(
            document(("<!-- rehearse: Bad_Id -->", strict("true"))), ["Bad_Id"])))

    def test_a_rehearsed_or_multi_command_block_must_be_strict(self):
        loose = document(("<!-- rehearse: one -->", "echo a\necho b"))
        self.assertIn("is not a single bash -euo pipefail", " ".join(runbook_blocks.problems(loose, ["one"])))
        several = document(("<!-- not-rehearsed: needs GitHub -->", "gh run list\ngh run watch 1"))
        self.assertIn("has more than one command", " ".join(runbook_blocks.problems(several, [])))
        single = document(("<!-- not-rehearsed: needs GitHub -->", "gh variable set X --body false \\\n  --repo r"))
        self.assertEqual([], runbook_blocks.problems(single, []))
        # A second EOF line would end the script early.
        early = document(("<!-- rehearse: one -->", strict("echo a", "EOF", "echo b")))
        self.assertIn("is not a single", " ".join(runbook_blocks.problems(early, ["one"])))

    def test_a_strict_block_guards_every_documented_variable_it_reads(self):
        unguarded = document(("<!-- rehearse: one -->", strict(': "${ns:?}"', 'echo "$ns" "$RECORD/x"')))
        self.assertIn("reads RECORD without a guard", " ".join(runbook_blocks.problems(unguarded, ["one"])))
        # kubectl and Helm read the deployer kubeconfig, so a block that runs them guards it.
        kubectl = document(("<!-- rehearse: one -->", strict(': "${ns:?}"', 'kubectl --namespace "$ns" get pods')))
        self.assertIn("reads KUBECONFIG without a guard", " ".join(runbook_blocks.problems(kubectl, ["one"])))
        substitution = document(("<!-- rehearse: one -->", strict('x=$(helm list -o json)', 'echo "$x"')))
        self.assertIn("reads KUBECONFIG", " ".join(runbook_blocks.problems(substitution, ["one"])))
        # An administrator step names its kubeconfig on the command line and needs no deployer one.
        admin = document(("<!-- rehearse: one -->", strict(
            ': "${ADMIN_KUBECONFIG:?}" "${ADMIN_CONTEXT:?}"',
            'kubectl --kubeconfig "$ADMIN_KUBECONFIG" --context "$ADMIN_CONTEXT" get events')))
        self.assertEqual([], runbook_blocks.problems(admin, ["one"]))
        defaulted = document(("<!-- rehearse: one -->", strict(': "${FROM:=baseline}" "${RECORD:?}"', 'cat "$RECORD/$FROM"')))
        self.assertEqual([], runbook_blocks.problems(defaulted, ["one"]))
        # Variables the runbook does not document are the block's own.
        local = document(("<!-- rehearse: one -->", strict(': "${RECORD:?}"', 'name=x', 'echo "$name" "$RECORD"')))
        self.assertEqual([], runbook_blocks.problems(local, ["one"]))

    def test_a_session_block_sets_shell_state_only(self):
        fine = document(("<!-- session: shell state -->",
                         'read -rp "Run ID: " RUN_ID && export RUN_ID\n'
                         'ADMIN_CONTEXT=$(kubectl --kubeconfig "$ADMIN_KUBECONFIG" config current-context)'))
        self.assertEqual([], runbook_blocks.problems(fine, []))
        for code in ('kubectl --namespace default get pods', 'x=$(helm list)', 'gh workflow run ci.yml'):
            with self.subTest(code=code):
                found = runbook_blocks.problems(document(("<!-- session: shell state -->", code)), [])
                self.assertIn("runs a cluster or GitHub command", " ".join(found))
        wrapped = document(("<!-- session: shell state -->", strict("echo a")))
        self.assertIn("runs a strict script", " ".join(runbook_blocks.problems(wrapped, [])))

    def test_blocks_outside_the_section_and_other_languages_are_not_checked(self):
        text = document(("<!-- rehearse: one -->", strict("echo a"))).replace(
            "## After", "```text\nnot code\n```\n\n## After")
        self.assertEqual([], runbook_blocks.problems(text, ["one"]))
        self.assertEqual({"one": strict("echo a") + "\n"}, runbook_blocks.rehearsed(text))

    def test_indented_blocks_in_a_list_item_are_read_without_their_indentation(self):
        text = document().replace(
            "## After", "1. A step:\n\n   <!-- rehearse: listed -->\n   ```bash\n   bash -euo pipefail <<'EOF'\n"
            "   if true; then\n     echo in\n   fi\n   EOF\n   ```\n\n## After")
        self.assertEqual({"listed": strict("if true; then", "  echo in", "fi") + "\n"}, runbook_blocks.rehearsed(text))

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
            "kubectl --namespace opensearch delete pvc data-0": "PersistentVolumeClaim or a namespace",
            "kubectl delete persistentvolumeclaim/data-0 -n opensearch": "PersistentVolumeClaim or a namespace",
            "kubectl delete --wait=true namespace opensearch": "PersistentVolumeClaim or a namespace",
            "kubectl delete ns otel-demo": "PersistentVolumeClaim or a namespace",
            "kubectl -n opensearch delete statefulset x --cascade=foreground": "foreground or background",
            "kubectl -n opensearch delete statefulset x --cascade background": "foreground or background",
            'helm rollback "$release" 3 --namespace "$ns" --force': "forces a Helm rollback",
            'helm rollback "$release" 3 --force-replace': "forces a Helm rollback",
            "kubectl -n opensearch replace --force -f sts.yaml": "replaces an object by force",
        }
        for code, why in cases.items():
            for marker in ("<!-- rehearse: one -->", "<!-- not-rehearsed: needs GitHub -->"):
                with self.subTest(code=code, marker=marker):
                    steps = ["one"] if "rehearse:" in marker else []
                    found = runbook_blocks.problems(document((marker, code)), steps)
                    self.assertTrue(any(why in problem for problem in found), found)

    def test_named_deletes_orphan_cascades_and_selector_reads_are_allowed(self):
        allowed = [
            'kubectl --namespace "$ns" delete secret "$secret"',
            "kubectl --namespace \"$ns\" get secret --selector owner=helm,name=demo-resources \\\n  --output name",
            "helm history demo-resources --namespace \"$ns\" --max 1 2>&1 | grep 'release: not found'",
            'helm rollback "$release" "$REVISION" --namespace "$ns" --wait=watcher',
            "kubectl --namespace opensearch delete statefulset opensearch-cluster-single --cascade=orphan",
            "kubectl --namespace opensearch delete pod opensearch-cluster-single-0",
            'kubectl --namespace "$ns" apply --filename reviewed-object.json',
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
