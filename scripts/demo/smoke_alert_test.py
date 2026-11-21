# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""State-change rules for the scheduled smoke check alerts."""

import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

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


class ReadRunsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'runs.json'

    def test_reads_the_run_list(self):
        self.path.write_text(json.dumps({'total_count': 1, 'workflow_runs': [{'id': 1}]}))
        self.assertEqual([{'id': 1}], smoke_alert.read_runs(self.path))

    def test_unusable_input_returns_none(self):
        self.assertIsNone(smoke_alert.read_runs(Path(self.temp.name) / 'missing.json'))
        for content in ('', 'not json', '[]', '{}', '{"workflow_runs": null}', '{"workflow_runs": "x"}', 'null'):
            with self.subTest(content=content):
                self.path.write_text(content)
                self.assertIsNone(smoke_alert.read_runs(self.path))


def run(run_id, conclusion, updated_at, attempt=1):
    return {'id': run_id, 'conclusion': conclusion, 'updated_at': updated_at, 'run_attempt': attempt}


def attempts(table):
    """A fake attempts API: table maps (run id, attempt) to (conclusion, updated_at)."""
    calls = []

    def fetch(run_id, attempt):
        calls.append((run_id, attempt))
        if (run_id, attempt) not in table:
            raise OSError('not found')
        conclusion, updated_at = table[(run_id, attempt)]
        return {'conclusion': conclusion, 'updated_at': updated_at}
    fetch.calls = calls
    return fetch


class BuildHistoryTests(unittest.TestCase):
    def test_orders_conclusive_runs_and_leaves_out_the_current_run(self):
        runs = [run(4, 'failure', 'T4'), run(3, 'cancelled', 'T3'), run(2, 'failure', 'T2'), run(1, 'success', 'T1')]
        fetch = attempts({})
        self.assertEqual(['failure', 'success'], smoke_alert.build_history(runs, 4, 1, fetch))
        self.assertEqual(['failure', 'failure', 'success'], smoke_alert.build_history(runs, 3, 1, fetch))
        self.assertEqual([], fetch.calls)

    def test_missing_runs_and_malformed_entries_are_tolerated(self):
        self.assertEqual([], smoke_alert.build_history(None, 5, 1, attempts({})))
        runs = ['x', None, {'id': 1}, run(2, 'failure', 'T2')]
        self.assertEqual(['failure'], smoke_alert.build_history(runs, 9, 1, attempts({})))

    def test_ties_and_missing_timestamps_keep_the_list_order(self):
        runs = [{'id': 3, 'conclusion': 'failure'}, {'id': 2, 'conclusion': 'success'},
                {'id': 1, 'conclusion': 'failure'}]
        self.assertEqual(['failure', 'success', 'failure'], smoke_alert.build_history(runs, 9, 1, attempts({})))

    def test_a_cancelled_last_attempt_keeps_the_failure_of_its_first_attempt(self):
        # Run 5 failed (and alerted), then its rerun was cancelled; run 6 now succeeds.
        runs = [run(5, 'cancelled', 'T5b', attempt=2), run(4, 'success', 'T4')]
        fetch = attempts({(5, 1): ('failure', 'T5a')})
        history = smoke_alert.build_history(runs, 6, 1, fetch)
        self.assertEqual(['failure', 'success'], history)
        self.assertEqual(smoke_alert.RECOVERED, decide('success', 'schedule', history))

    def test_the_latest_conclusive_attempt_is_found_across_three_attempts(self):
        runs = [run(5, 'cancelled', 'T5c', attempt=3), run(4, 'success', 'T4')]
        fetch = attempts({(5, 2): ('cancelled', 'T5b'), (5, 1): ('failure', 'T5a')})
        self.assertEqual(['failure', 'success'], smoke_alert.build_history(runs, 6, 1, fetch))
        self.assertEqual([(5, 2), (5, 1)], fetch.calls)

    def test_an_unreadable_attempt_loses_only_that_attempt(self):
        runs = [run(5, 'cancelled', 'T5c', attempt=3), run(4, 'success', 'T4')]
        fetch = attempts({(5, 1): ('failure', 'T5a')})
        self.assertEqual(['failure', 'success'], smoke_alert.build_history(runs, 6, 1, fetch))
        self.assertEqual(['success'], smoke_alert.build_history(runs, 6, 1, attempts({})))

    def test_a_completed_rerun_counts_by_its_final_attempt(self):
        runs = [run(5, 'success', 'T5b', attempt=2), run(4, 'success', 'T4')]
        fetch = attempts({})
        self.assertEqual(['success', 'success'], smoke_alert.build_history(runs, 6, 1, fetch))
        self.assertEqual([], fetch.calls)

    def test_the_current_rerun_counts_its_earlier_attempts(self):
        runs = [run(4, 'success', 'T4')]
        fetch = attempts({(5, 2): ('cancelled', 'T5b'), (5, 1): ('failure', 'T5a')})
        history = smoke_alert.build_history(runs, 5, 3, fetch)
        self.assertEqual(['failure', 'success'], history)
        self.assertEqual(smoke_alert.RECOVERED, decide('success', 'schedule', history))

    def test_the_first_attempt_reads_no_earlier_attempts(self):
        fetch = attempts({})
        smoke_alert.build_history([run(4, 'success', 'T4')], 5, 1, fetch)
        self.assertEqual([], fetch.calls)

    def test_a_rerun_of_an_older_failure_does_not_override_a_newer_success(self):
        # A failed (T1), B succeeded and recovered (T2), then A is rerun.
        runs = [run(2, 'success', 'T2')]
        fetch = attempts({(1, 1): ('failure', 'T1')})
        history = smoke_alert.build_history(runs, 1, 2, fetch)
        self.assertEqual(['success', 'failure'], history)
        self.assertEqual(smoke_alert.NONE, decide('success', 'schedule', history))
        # A failed rerun is a new outage: the newest result before it is healthy.
        self.assertEqual(smoke_alert.FAILED, decide('failure', 'schedule', history))

    def test_a_successful_rerun_of_the_newest_failure_recovers_once(self):
        runs = [run(1, 'success', 'T0')]
        history = smoke_alert.build_history(runs, 2, 2, attempts({(2, 1): ('failure', 'T2')}))
        self.assertEqual(smoke_alert.RECOVERED, decide('success', 'schedule', history))
        # Afterwards run 2 reads as healthy, so the next run is silent.
        after = smoke_alert.build_history([run(2, 'success', 'T3', attempt=2), run(1, 'success', 'T0')], 3, 1,
                                          attempts({}))
        self.assertEqual(smoke_alert.NONE, decide('success', 'schedule', after))


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

    def test_rerun_attempts_are_read_with_the_github_cli(self):
        # The fake gh answers the attempts endpoint for run 9, attempt 1.
        with tempfile.TemporaryDirectory() as temp:
            gh = Path(temp) / 'gh'
            gh.write_text('#!/bin/sh\ncase "$*" in\n*/runs/9/attempts/1) '
                          'echo \'{"conclusion": "failure", "updated_at": "T1"}\';;\n*) exit 1;;\nesac\n')
            gh.chmod(0o755)
            path = Path(temp) / 'runs.json'
            path.write_text(json.dumps({'workflow_runs': [run(8, 'success', 'T0')]}))
            args = ('--event', 'schedule', '--history', str(path), '--run-id', '9')
            with mock.patch.dict(os.environ, {'PATH': temp + os.pathsep + os.environ['PATH'],
                                              'GITHUB_REPOSITORY': 'jaegertracing/jaeger-demo'}):
                self.assertEqual('recovered\n', self.run_main('--result', 'success', *args, '--run-attempt', '2'))
                self.assertEqual('none\n', self.run_main('--result', 'success', *args, '--run-attempt', '1'))

    def test_a_rerun_without_the_github_cli_still_decides(self):
        with mock.patch.dict(os.environ, {'GITHUB_REPOSITORY': 'jaegertracing/jaeger-demo', 'PATH': '/nonexistent'}):
            self.assertEqual('failed\n', self.run_main(
                '--result', 'failure', '--event', 'schedule', '--run-id', '9', '--run-attempt', '3'))
            self.assertEqual('none\n', self.run_main(
                '--result', 'success', '--event', 'schedule', '--run-id', '9', '--run-attempt', '3'))

    def test_rejects_an_unknown_result(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            smoke_alert.main(['--result', 'skipped', '--event', 'schedule'])


if __name__ == '__main__':
    unittest.main()
