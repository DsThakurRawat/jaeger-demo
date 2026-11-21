# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Check that guarded-file changes are detected the way the executor compares them."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import guarded_changes  # noqa: E402
import scheduled_inputs  # noqa: E402


OTEL = "examples/otel-demo/deploy-all.sh"
UNGUARDED = "README.md"
SCRIPT = Path(__file__).resolve().with_name("guarded_changes.py")
DEPLOY = (
    '#!/usr/bin/env bash\n'
    'OTEL_DEMO_CHART_VERSION="0.40.9"\n'
    'OPENSEARCH_CHART_VERSION="3.7.0"\n'
    'OPENSEARCH_DASHBOARDS_CHART_VERSION="3.7.0"\n'
    'echo deploy\n'
)


class GuardedChangesTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.repo = Path(self.directory.name)
        self.git("init", "--quiet")
        self.write(OTEL, DEPLOY)
        self.write(UNGUARDED, "docs\n")
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
        self.git("commit", "--quiet", "-m", message)
        return self.git("rev-parse", "HEAD")

    def changed(self):
        return guarded_changes.changed(self.repo, self.base, self.git("rev-parse", "HEAD"))

    def test_pin_only_chart_bump_is_not_reported(self):
        self.write(OTEL, DEPLOY.replace('"0.40.9"', '"0.41.0"').replace('OPENSEARCH_CHART_VERSION="3.7.0"',
                                                                         'OPENSEARCH_CHART_VERSION="3.8.0"'))
        self.commit("bump")
        self.assertEqual([], self.changed())

    def test_edit_outside_the_pin_lines_is_reported(self):
        self.write(OTEL, DEPLOY.replace("echo deploy", "echo deploy now"))
        self.commit("edit")
        self.assertEqual([OTEL], self.changed())

    def test_pin_bump_with_another_edit_is_reported(self):
        self.write(OTEL, DEPLOY.replace('"0.40.9"', '"0.41.0"').replace("echo deploy", "echo other"))
        self.commit("both")
        self.assertEqual([OTEL], self.changed())

    def test_unguarded_file_is_not_reported(self):
        self.write(UNGUARDED, "changed docs\n")
        self.commit("docs")
        self.assertEqual([], self.changed())

    def test_added_and_deleted_guarded_files_are_reported(self):
        added = "scripts/demo/verify_trace.py"
        self.write(added, "x = 1\n")
        with_added = self.commit("add")
        self.assertEqual([added], guarded_changes.changed(self.repo, self.base, with_added))
        (self.repo / OTEL).unlink()
        self.commit("delete")
        self.assertEqual([OTEL, added], sorted(self.changed()))

    def test_list_comes_from_scheduled_inputs(self):
        self.write(UNGUARDED, "changed docs\n")
        self.commit("docs")
        self.assertEqual([], self.changed())
        with mock.patch.object(scheduled_inputs, "GUARDED", (UNGUARDED,)):
            self.assertEqual([UNGUARDED], self.changed())

    def test_command_line_prints_paths_and_rejects_unknown_commits(self):
        self.write(OTEL, DEPLOY.replace("echo deploy", "echo deploy now"))
        head = self.commit("edit")
        run = lambda *args: subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.repo), *args],  # noqa: E731
                                           capture_output=True, text=True)
        result = run(self.base, head)
        self.assertEqual((0, OTEL + "\n"), (result.returncode, result.stdout))
        self.assertEqual(2, run("0" * 40, head).returncode)


if __name__ == "__main__":
    unittest.main()
