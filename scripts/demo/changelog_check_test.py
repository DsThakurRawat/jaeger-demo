# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Check that pull requests need a changelog entry and that the changelog keeps its structure."""

import contextlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import changelog_check  # noqa: E402


TODAY = "2026-11-21"
BASE_LOG = """# Changelog

Preamble with a - dash that is not an entry.

## 2026-11-20

### Fixed

- Older fix.
  ([#1](https://github.com/jaegertracing/jaeger-demo/pull/1))
"""
ENTRY = """## 2026-11-21

### Added

- New thing.
  ([#9](https://github.com/jaegertracing/jaeger-demo/pull/9),
  fixes [#8](https://github.com/jaegertracing/jaeger-demo/issues/8))

"""


def with_entry(log, entry=ENTRY):
    return log.replace("## 2026-11-20", entry + "## 2026-11-20", 1)


class ChangelogCheckTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        self.git("init", "--quiet")
        self.write("CHANGELOG.md", BASE_LOG)
        self.write("scripts/demo/deploy.py", "print('deploy')\n")
        self.write("scripts/demo/deploy_test.py", "# test\n")
        self.base = self.commit("base")

    def git(self, *args):
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        return subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=t", "-c", "user.email=t@example.com",
                               "-c", "commit.gpgsign=false", *args],
                              check=True, capture_output=True, text=True, env=env).stdout.strip()

    def write(self, name, text):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def commit(self, message):
        self.git("add", "-A")
        self.git("commit", "--quiet", "--allow-empty", "-m", message)
        return self.git("rev-parse", "HEAD")

    def check(self, **kwargs):
        kwargs.setdefault("today", TODAY)
        return changelog_check.check(self.repo, self.base, self.commit("head"), **kwargs)

    def test_a_notable_change_without_an_entry_fails_and_names_the_file(self):
        self.write("scripts/demo/deploy.py", "print('changed')\n")
        errors, _ = self.check()
        self.assertEqual(len(errors), 1)
        self.assertIn("scripts/demo/deploy.py", errors[0])
        self.assertIn("skip-changelog", errors[0])

    def test_a_notable_change_with_a_dated_entry_passes(self):
        self.write("scripts/demo/deploy.py", "print('changed')\n")
        self.write("CHANGELOG.md", with_entry(BASE_LOG))
        self.assertEqual(self.check(pr=9), ([], []))

    def test_an_entry_under_an_existing_heading_passes(self):
        self.write("scripts/demo/deploy.py", "print('changed')\n")
        self.write("CHANGELOG.md", BASE_LOG.replace("- Older fix.", "- Newer fix.\n- Older fix.", 1))
        errors, warnings = self.check()
        self.assertEqual(errors, [])
        self.assertIn("2026-11-20", warnings[0])

    def test_the_skip_label_allows_a_change_without_an_entry(self):
        self.write("scripts/demo/deploy.py", "print('changed')\n")
        self.assertEqual(self.check(skip=True), ([], []))

    def test_test_only_changes_need_no_entry(self):
        self.write("scripts/demo/deploy_test.py", "# more tests\n")
        self.write("examples/otel-demo/deploy-all.test.sh", "# shell test\n")
        self.assertEqual(self.check(), ([], []))

    def test_correcting_an_existing_entry_alone_needs_no_new_entry(self):
        self.write("CHANGELOG.md", BASE_LOG.replace("Older fix.", "Older, corrected fix."))
        self.assertEqual(self.check(), ([], []))

    def test_rewording_an_existing_bullet_is_not_an_entry_for_other_changes(self):
        self.write("scripts/demo/deploy.py", "print('changed')\n")
        self.write("CHANGELOG.md", BASE_LOG.replace("- Older fix.", "- Older, corrected fix."))
        errors, _ = self.check()
        self.assertEqual(len(errors), 1)
        self.assertIn("scripts/demo/deploy.py", errors[0])

    def test_the_first_changelog_counts_as_an_entry(self):
        self.git("rm", "--quiet", "CHANGELOG.md")
        self.base = self.commit("no changelog")
        self.write("scripts/demo/deploy.py", "print('changed')\n")
        self.write("CHANGELOG.md", with_entry(BASE_LOG))
        errors, _ = self.check()
        self.assertEqual(errors, [])

    def test_correcting_an_existing_entry_does_not_count_as_an_entry_for_other_changes(self):
        self.write("scripts/demo/deploy.py", "print('changed')\n")
        self.write("CHANGELOG.md", BASE_LOG.replace("  ([#1]", "  (see [#1]"))
        errors, _ = self.check()
        self.assertEqual(len(errors), 1)

    def test_a_deleted_file_needs_an_entry(self):
        (self.repo / "scripts/demo/deploy.py").unlink()
        errors, _ = self.check()
        self.assertIn("scripts/demo/deploy.py", errors[0])

    def test_a_long_file_list_is_shortened(self):
        for index in range(7):
            self.write(f"docs/page{index}.md", "text\n")
        errors, _ = self.check()
        self.assertIn("and 2 more", errors[0])

    def test_skip_still_validates_the_changelog_structure(self):
        self.write("CHANGELOG.md", with_entry(BASE_LOG, "## 2026-11-19\n\n### Added\n\n- Out of order.\n\n"))
        errors, _ = self.check(skip=True)
        self.assertTrue(any("must be earlier than" in error for error in errors), errors)

    def test_structure_errors_are_reported(self):
        cases = {
            "## November 21\n\n### Added\n\n- Entry.\n\n": "expected '## YYYY-MM-DD'",
            "## 2026-13-01\n\n### Added\n\n- Entry.\n\n": "is not a valid date",
            "## 2026-11-20\n\n### Added\n\n- Entry.\n\n": "no duplicates",
            "## 2026-11-21\n\n### Improved\n\n- Entry.\n\n": "unknown category 'Improved'",
            "## 2026-11-21\n\n### Added\n\n- One.\n\n### Added\n\n- Two.\n\n": "appears twice",
            "## 2026-11-21\n\n- Uncategorized.\n\n": "not under a category heading",
        }
        for entry, expected in cases.items():
            with self.subTest(expected=expected):
                errors, _ = changelog_check.parse(with_entry(BASE_LOG, entry))
                self.assertTrue(any(expected in error for error in errors), errors)

    def test_a_category_before_any_date_is_reported(self):
        errors, _ = changelog_check.parse("# Changelog\n\n### Added\n\n- Entry.\n")
        self.assertTrue(any("not under a date heading" in error for error in errors), errors)

    def test_continuation_lines_are_not_entries(self):
        _, sections = changelog_check.parse(with_entry(BASE_LOG))
        lines = with_entry(BASE_LOG).splitlines()
        self.assertEqual([lines[number - 1] for number in sections], ["- New thing.", "- Older fix."])

    def test_an_entry_dated_other_than_today_warns(self):
        self.write("CHANGELOG.md", with_entry(BASE_LOG))
        errors, warnings = self.check(today="2026-11-22")
        self.assertEqual(errors, [])
        self.assertEqual(len(warnings), 1)
        self.assertIn("2026-11-21", warnings[0])

    def test_a_missing_pull_request_link_warns(self):
        self.write("CHANGELOG.md", with_entry(BASE_LOG))
        _, warnings = self.check(pr=42)
        self.assertEqual(warnings, ["No CHANGELOG.md entry links this pull request (#42) yet."])

    def test_deleting_the_changelog_fails(self):
        (self.repo / "CHANGELOG.md").unlink()
        errors, _ = self.check()
        self.assertEqual(errors, ["CHANGELOG.md was deleted"])

    def test_an_unknown_commit_is_rejected(self):
        with self.assertRaisesRegex(changelog_check.CheckError, "Unknown commit"):
            changelog_check.check(self.repo, "0" * 40, self.base)

    def test_command_line_exit_codes_and_annotations(self):
        self.write("scripts/demo/deploy.py", "print('changed')\n")
        head = self.commit("head")
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), contextlib.redirect_stdout(output):
            self.assertEqual(changelog_check.main(["--repo", str(self.repo), self.base, head]), 1)
            self.assertEqual(changelog_check.main(["--repo", str(self.repo), "--skip", self.base, head]), 0)
        self.assertTrue(output.getvalue().startswith("::error::No CHANGELOG.md entry"))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(changelog_check.main(["--repo", str(self.repo), "0" * 40, head]), 2)


class RepositoryChangelogTests(unittest.TestCase):
    def test_the_repository_changelog_is_well_formed(self):
        text = (changelog_check.REPO / changelog_check.CHANGELOG).read_text()
        errors, sections = changelog_check.parse(text)
        self.assertEqual(errors, [])
        self.assertTrue(sections)


if __name__ == "__main__":
    unittest.main()
