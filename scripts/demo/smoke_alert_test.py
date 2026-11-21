# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""State-change rules for the scheduled smoke check alerts."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest

import smoke_alert
from smoke_alert import decide, FAILED, NONE, RECOVERED


class DecideScheduledTests(unittest.TestCase):
    def test_first_failure_after_success_alerts(self):
        self.assertEqual(FAILED, decide('failure', 'schedule', ['success', 'success']))

    def test_continuing_outage_stays_silent(self):
        self.assertEqual(NONE, decide('failure', 'schedule', ['failure', 'failure', 'success']))

    def test_recovery_after_failure_is_posted_once(self):
        self.assertEqual(RECOVERED, decide('success', 'schedule', ['failure', 'failure', 'success']))
        self.assertEqual(NONE, decide('success', 'schedule', ['success', 'failure']))

    def test_healthy_runs_stay_silent(self):
        self.assertEqual(NONE, decide('success', 'schedule', ['success', 'success']))

    def test_sustained_outage_alerts_once_and_recovers_once(self):
        history = []
        posts = []
        for result in ['success', 'failure', 'failure', 'failure', 'failure', 'success', 'success']:
            posts.append(decide(result, 'schedule', history))
            history.insert(0, result)
        self.assertEqual([NONE, FAILED, NONE, NONE, NONE, RECOVERED, NONE], posts)

    def test_cancelled_and_skipped_runs_are_skipped_when_looking_back(self):
        self.assertEqual(NONE, decide('failure', 'schedule', ['cancelled', 'skipped', 'failure', 'success']))
        self.assertEqual(FAILED, decide('failure', 'schedule', ['cancelled', 'skipped', 'success', 'failure']))
        self.assertEqual(RECOVERED, decide('success', 'schedule', ['skipped', 'cancelled', 'failure', 'success']))
        self.assertEqual(NONE, decide('success', 'schedule', ['cancelled', 'success', 'failure']))

    def test_other_conclusions_say_nothing_about_health(self):
        for other in ('timed_out', 'startup_failure', 'action_required', 'neutral', 'stale', None):
            with self.subTest(other=other):
                self.assertEqual(FAILED, decide('failure', 'schedule', [other, 'success']))
                self.assertEqual(NONE, decide('failure', 'schedule', [other, 'failure']))

    def test_unreadable_or_empty_history_fails_open(self):
        for history in (None, [], ['cancelled', 'skipped']):
            with self.subTest(history=history):
                self.assertEqual(FAILED, decide('failure', 'schedule', history))
                self.assertEqual(NONE, decide('success', 'schedule', history))

    def test_cancelled_scheduled_run_is_silent(self):
        for history in (None, [], ['success'], ['failure']):
            with self.subTest(history=history):
                self.assertEqual(NONE, decide('cancelled', 'schedule', history))


class DecideManualTests(unittest.TestCase):
    def test_manual_failure_always_alerts(self):
        for history in (None, [], ['failure'], ['success'], ['failure', 'failure']):
            with self.subTest(history=history):
                self.assertEqual(FAILED, decide('failure', 'workflow_dispatch', history))

    def test_manual_success_never_recovers(self):
        for history in (None, [], ['failure'], ['cancelled', 'failure'], ['success']):
            with self.subTest(history=history):
                self.assertEqual(NONE, decide('success', 'workflow_dispatch', history))

    def test_manual_cancellation_alerts(self):
        self.assertEqual(FAILED, decide('cancelled', 'workflow_dispatch', ['failure']))
        self.assertEqual(FAILED, decide('cancelled', 'workflow_dispatch', None))

    def test_only_schedule_is_deduplicated(self):
        self.assertEqual(FAILED, decide('failure', 'push', ['failure']))

    def test_unknown_result_is_rejected(self):
        with self.assertRaises(ValueError):
            decide('skipped', 'schedule', [])


class ReadHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'runs.json'

    def write(self, content):
        self.path.write_text(content if isinstance(content, str) else json.dumps(content))

    def test_reads_conclusions_in_order_without_the_current_run(self):
        self.write({'total_count': 4, 'workflow_runs': [
            {'id': 4, 'conclusion': 'failure'}, {'id': 3, 'conclusion': 'cancelled'},
            {'id': 2, 'conclusion': 'failure'}, {'id': 1, 'conclusion': 'success'}]})
        self.assertEqual(['cancelled', 'failure', 'success'], smoke_alert.read_history(self.path, '4'))
        self.assertEqual(['failure', 'cancelled', 'failure', 'success'], smoke_alert.read_history(self.path))

    def test_unusable_input_returns_none(self):
        self.assertIsNone(smoke_alert.read_history(Path(self.temp.name) / 'missing.json'))
        for content in ('', 'not json', '[]', '{}', '{"workflow_runs": null}', '{"workflow_runs": "x"}', 'null'):
            with self.subTest(content=content):
                self.write(content)
                self.assertIsNone(smoke_alert.read_history(self.path))

    def test_malformed_entries_are_ignored(self):
        self.write({'workflow_runs': ['x', None, {'id': 1}, {'id': 2, 'conclusion': 'failure'}]})
        self.assertEqual([None, 'failure'], smoke_alert.read_history(self.path))


class CommandLineTests(unittest.TestCase):
    def run_main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = smoke_alert.main(list(argv))
        self.assertEqual(0, status)
        return out.getvalue()

    def test_prints_the_decision_from_a_history_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'runs.json'
            path.write_text(json.dumps({'workflow_runs': [
                {'id': 9, 'conclusion': 'failure'}, {'id': 8, 'conclusion': 'success'}]}))
            args = ('--event', 'schedule', '--history', str(path))
            self.assertEqual('none\n', self.run_main('--result', 'failure', *args))
            self.assertEqual('failed\n', self.run_main('--result', 'failure', *args, '--run-id', '9'))
            self.assertEqual('none\n', self.run_main('--result', 'success', *args, '--run-id', '9'))
            self.assertEqual('recovered\n', self.run_main('--result', 'success', *args))

    def test_missing_history_fails_open(self):
        self.assertEqual('failed\n', self.run_main('--result', 'failure', '--event', 'schedule'))
        self.assertEqual('failed\n', self.run_main(
            '--result', 'failure', '--event', 'schedule', '--history', '/nonexistent/runs.json'))
        self.assertEqual('none\n', self.run_main('--result', 'success', '--event', 'schedule'))

    def test_rejects_an_unknown_result(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            smoke_alert.main(['--result', 'skipped', '--event', 'schedule'])


if __name__ == '__main__':
    unittest.main()
