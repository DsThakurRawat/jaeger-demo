# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Keep the scheduled public smoke check credential-free and bounded."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / '.github/workflows/demo-smoke.yml').read_text()
CI = (ROOT / '.github/workflows/ci.yml').read_text()
DEPLOY = (ROOT / '.github/workflows/ci-deploy-demo.yml').read_text()
JOB = yaml.safe_load(WORKFLOW)['jobs']['smoke']
STEPS = {step['name']: step for step in JOB['steps'] if 'name' in step}
# One stack_health.py run makes this many sequential requests, each with the
# 15 s socket timeout set in stack_health.fetch. stack_health_test.py pins the
# counts (REQUESTS_PER_RUN), so a new request fails there until both are recounted.
REQUESTS_PER_RUN = {'oci': 9, 'otel': 10}
WORST_CASE_RUN_SECONDS = {stack: count * 15 for stack, count in REQUESTS_PER_RUN.items()}


def run_step(step, stubs, **env):
    """Run a step's shell block with stub commands first on PATH.

    ``stubs`` maps a command name to the body of its stub script. Returns the
    result, the stub call log lines and the lines written to GITHUB_OUTPUT.
    """
    script = step['run']
    assert '${{' not in script
    with tempfile.TemporaryDirectory() as temp:
        for name, body in stubs.items():
            stub = Path(temp) / name
            stub.write_text('#!/bin/sh\necho "' + name + ' $*" >> "$TEST_LOG"\n' + body + '\n')
            stub.chmod(0o755)
        log, output = Path(temp) / 'calls.log', Path(temp) / 'output'
        output.touch()
        env = dict(os.environ, PATH=temp + os.pathsep + os.environ['PATH'], TEST_LOG=str(log),
                   TEST_DIR=temp, GITHUB_OUTPUT=str(output), RUNNER_TEMP=temp,
                   GITHUB_REPOSITORY='jaegertracing/jaeger-demo', **env)
        result = subprocess.run(['bash', '-e', '-c', script], env=env, cwd=ROOT, text=True, capture_output=True)
        calls = log.read_text().splitlines() if log.exists() else []
        return result, calls, output.read_text().splitlines()


# Fails the first $TEST_FAILS runs, then succeeds.
FLAKY_PYTHON = (
    'n=$(cat "$TEST_DIR/count" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "$TEST_DIR/count"\n'
    '[ "$n" -gt "$TEST_FAILS" ]')
PASS_THROUGH_TIMEOUT = 'shift; exec "$@"'


def pinned(text, action):
    return set(re.findall(re.escape(action) + r'@([0-9a-f]{40})', text))


class SmokeWorkflowTests(unittest.TestCase):
    def test_has_no_oci_credentials_or_environment(self):
        self.assertNotIn('OCI_', WORKFLOW)
        self.assertIsNone(re.search(r'^\s*environment:', WORKFLOW, re.M))

    def test_permissions_are_read_only(self):
        self.assertIn('\npermissions:\n  contents: read\n', WORKFLOW)
        self.assertNotRegex(WORKFLOW, r'(?m)^\s*[a-z-]+: write\b')

    def test_triggers_every_thirty_minutes_and_manually(self):
        self.assertIn("- cron: '*/30 * * * *'", WORKFLOW)
        self.assertIn('workflow_dispatch:', WORKFLOW)

    def test_is_guarded_and_bounded(self):
        self.assertEqual(WORKFLOW.count("if: github.repository == 'jaegertracing/jaeger-demo'"), 1)
        self.assertRegex(WORKFLOW, r'(?m)^    timeout-minutes: \d+$')

    def test_scheduled_runs_need_the_opt_in_switch(self):
        condition = re.search(r'(?m)^    if: (.+)$', WORKFLOW)[1]
        cases = {('schedule', 'true'): True, ('schedule', ''): False, ('schedule', 'false'): False,
                 ('workflow_dispatch', ''): True, ('workflow_dispatch', 'false'): True}
        for (event, switch), expected in cases.items():
            with self.subTest(event=event, switch=switch):
                expression = condition.replace('github.repository', repr('jaegertracing/jaeger-demo'))
                expression = expression.replace('github.event_name', repr(event))
                expression = expression.replace('vars.DEMO_SMOKE_ENABLED', repr(switch))
                expression = expression.replace('&&', 'and').replace('||', 'or')
                self.assertEqual(expected, eval(expression, {'__builtins__': {}}, {}))

    def test_checks_both_stacks(self):
        for stack in ('oci', 'otel'):
            self.assertIn(f'scripts/demo/stack_health.py --stack {stack}', WORKFLOW)

    def test_actions_use_the_pins_of_the_existing_workflows(self):
        checkout = pinned(WORKFLOW, 'actions/checkout')
        self.assertTrue(checkout)
        self.assertEqual(checkout, pinned(CI, 'actions/checkout'))
        self.assertIn('persist-credentials: false', WORKFLOW)
        slack = pinned(WORKFLOW, 'rtCamp/action-slack-notify')
        self.assertTrue(slack)
        self.assertEqual(slack, pinned(DEPLOY, 'rtCamp/action-slack-notify'))

    def test_uses_only_the_webhook_secret(self):
        self.assertEqual(set(re.findall(r'secrets\.(\w+)', WORKFLOW)), {'SLACK_WEBHOOK_URL'})


class RetryTests(unittest.TestCase):
    CHECKS = {'oci': 'Check the OCI demo', 'otel': 'Check the OpenTelemetry demo'}

    def attempt(self, stack, fails):
        return run_step(STEPS[self.CHECKS[stack]], {
            'python3': FLAKY_PYTHON, 'timeout': PASS_THROUGH_TIMEOUT, 'sleep': ''}, TEST_FAILS=str(fails))

    def test_a_transient_failure_does_not_fail_the_step(self):
        for stack in self.CHECKS:
            for fails in (0, 1, 2):
                with self.subTest(stack=stack, fails=fails):
                    result, calls, _ = self.attempt(stack, fails)
                    self.assertEqual(0, result.returncode, result.stderr)
                    runs = [call for call in calls if call.startswith('python3 ')]
                    self.assertEqual(fails + 1, len(runs))
                    self.assertTrue(all(call.endswith(f'--stack {stack}') for call in runs))
                    self.assertEqual(fails, len([call for call in calls if call.startswith('sleep ')]))

    def test_the_step_fails_only_after_three_attempts(self):
        for stack in self.CHECKS:
            with self.subTest(stack=stack):
                result, calls, _ = self.attempt(stack, 99)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual(3, len([call for call in calls if call.startswith('python3 ')]))
                self.assertEqual(2, len([call for call in calls if call.startswith('sleep ')]))

    def test_each_attempt_is_bounded_by_a_timeout_that_covers_one_run(self):
        for stack in self.CHECKS:
            with self.subTest(stack=stack):
                _, calls, _ = self.attempt(stack, 99)
                limits = [int(call.split()[1]) for call in calls if call.startswith('timeout ')]
                self.assertEqual(3, len(limits))
                for limit in limits:
                    # Above the worst case, so a run that is merely slow is not killed.
                    self.assertGreater(limit, WORST_CASE_RUN_SECONDS[stack])
                    self.assertLess(limit, 2 * WORST_CASE_RUN_SECONDS[stack])

    def test_pauses_between_attempts_are_short(self):
        _, calls, _ = self.attempt('oci', 99)
        pauses = [int(call.split()[1]) for call in calls if call.startswith('sleep ')]
        self.assertTrue(pauses and all(0 < pause <= 60 for pause in pauses))

    def test_a_hung_attempt_is_killed_and_retried(self):
        step = STEPS[self.CHECKS['oci']]
        result, calls, _ = run_step(step, {
            'python3': 'sleep 30', 'timeout': 'shift; shift; exit 124', 'sleep': ''})
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(3, len([call for call in calls if call.startswith('timeout ')]))

    def test_the_second_stack_runs_after_the_first_fails(self):
        self.assertEqual('${{ !cancelled() }}', STEPS[self.CHECKS['otel']]['if'])
        self.assertNotIn('if', STEPS[self.CHECKS['oci']])

    def test_the_job_limit_covers_the_worst_case(self):
        total = 0
        for stack in self.CHECKS:
            _, calls, _ = self.attempt(stack, 99)
            total += sum(int(call.split()[1]) for call in calls if call.startswith(('timeout ', 'sleep ')))
        # The alert decision is bounded by its own timeouts; checkout and the
        # Slack post are allowed 60 s together.
        decision = sum(int(seconds) for seconds in re.findall(r'\btimeout (\d+) ', STEPS['Decide whether to notify']['run']))
        self.assertEqual(90, decision)
        self.assertLessEqual(total + decision + 60, JOB['timeout-minutes'] * 60)
        self.assertGreater(total, 3 * sum(WORST_CASE_RUN_SECONDS.values()))


class AlertWiringTests(unittest.TestCase):
    DECIDE = STEPS['Decide whether to notify']
    FAILED = STEPS['Send Slack notification on failure']
    RECOVERED = STEPS['Send Slack notification on recovery']

    def test_job_can_read_actions_and_nothing_can_write(self):
        self.assertEqual({'actions': 'read', 'contents': 'read'}, JOB['permissions'])
        self.assertEqual({'contents': 'read'}, yaml.safe_load(WORKFLOW)['permissions'])
        self.assertNotIn('write', json.dumps(yaml.safe_load(WORKFLOW)['permissions']) + json.dumps(JOB['permissions']))

    def test_decision_step_always_runs_and_uses_the_workflow_token(self):
        self.assertEqual('alert', self.DECIDE['id'])
        self.assertEqual('always()', self.DECIDE['if'])
        self.assertEqual('${{ github.token }}', self.DECIDE['env']['GH_TOKEN'])
        self.assertEqual('${{ job.status }}', self.DECIDE['env']['RESULT'])
        self.assertIn('actions/workflows/demo-smoke.yml/runs?event=schedule&status=completed&per_page=10',
                      self.DECIDE['run'])

    def test_slack_steps_follow_the_helper_output_only(self):
        self.assertEqual("always() && steps.alert.outputs.action == 'failed'", self.FAILED['if'])
        self.assertEqual("always() && steps.alert.outputs.action == 'recovered'", self.RECOVERED['if'])
        slack = [step for step in JOB['steps'] if step.get('uses', '').startswith('rtCamp/action-slack-notify@')]
        self.assertEqual([self.FAILED, self.RECOVERED], slack)

    def test_recovery_message_is_green_and_titled_differently(self):
        self.assertEqual('danger', self.FAILED['env']['SLACK_COLOR'])
        self.assertEqual('good', self.RECOVERED['env']['SLACK_COLOR'])
        self.assertNotEqual(self.FAILED['env']['SLACK_TITLE'], self.RECOVERED['env']['SLACK_TITLE'])
        self.assertIn('Recovered', self.RECOVERED['env']['SLACK_TITLE'])
        for step in (self.FAILED, self.RECOVERED):
            self.assertEqual('${{ secrets.SLACK_WEBHOOK_URL }}', step['env']['SLACK_WEBHOOK'])

    def decide(self, result, event, runs, python=None, attempt=1, previous='', timeout=PASS_THROUGH_TIMEOUT):
        history = json.dumps({'workflow_runs': runs}) if runs is not None else None
        runs_gh = 'cat <<\'EOF\'\n' + history + '\nEOF' if history else 'exit 1'
        # The attempts endpoint answers with the previous attempt's run, or
        # fails when previous is None. smoke_alert.py makes that call itself.
        attempts_gh = 'exit 1' if previous is None else \
            'echo "{\\"conclusion\\": \\"$TEST_PREVIOUS\\", \\"updated_at\\": \\"T1\\"}"'
        gh = 'case "$*" in\n*"/attempts/"*)\n' + attempts_gh + '\n;;\n*)\n' + runs_gh + '\n;;\nesac'
        stubs = {'gh': gh, 'timeout': timeout}
        if python is not None:
            stubs['python3'] = python
        outcome, calls, output = run_step(self.DECIDE, stubs, RESULT=result, EVENT_NAME=event, RUN_ID='100',
                                          RUN_ATTEMPT=str(attempt), TEST_PREVIOUS=previous or '')
        self.assertEqual(0, outcome.returncode, outcome.stderr)
        self.assertEqual(1, len(output))
        return output[0], calls

    def test_lookup_reads_the_schedule_history_with_the_helper(self):
        runs = [{'id': 100, 'conclusion': 'failure'}, {'id': 99, 'conclusion': 'failure'},
                {'id': 98, 'conclusion': 'success'}]
        action, calls = self.decide('failure', 'schedule', runs)
        self.assertEqual('action=none', action)
        self.assertEqual(['gh api repos/jaegertracing/jaeger-demo/actions/workflows/demo-smoke.yml/runs'
                          '?event=schedule&status=completed&per_page=10'], [c for c in calls if c.startswith('gh ')])

    def test_the_decision_follows_the_state_change_rules(self):
        failure, success = {'id': 99, 'conclusion': 'failure'}, {'id': 99, 'conclusion': 'success'}
        cases = [('failure', 'schedule', [success], 'failed'), ('failure', 'schedule', [failure], 'none'),
                 ('success', 'schedule', [failure], 'recovered'), ('success', 'schedule', [success], 'none'),
                 ('cancelled', 'schedule', [success], 'none'),
                 ('failure', 'workflow_dispatch', [failure], 'failed'),
                 ('success', 'workflow_dispatch', [failure], 'none'),
                 ('cancelled', 'workflow_dispatch', [success], 'failed')]
        for result, event, runs, expected in cases:
            with self.subTest(result=result, event=event, previous=runs[0]['conclusion']):
                self.assertEqual(f'action={expected}', self.decide(result, event, runs)[0])

    def test_a_failed_history_lookup_does_not_hide_a_failure(self):
        for event in ('schedule', 'workflow_dispatch'):
            with self.subTest(event=event):
                self.assertEqual('action=failed', self.decide('failure', event, None)[0])
                self.assertEqual('action=none', self.decide('success', event, None)[0])

    def test_a_broken_helper_does_not_hide_a_failure(self):
        runs = [{'id': 99, 'conclusion': 'failure'}]
        cases = [('failure', 'schedule', 'failed'), ('success', 'schedule', 'none'),
                 ('cancelled', 'schedule', 'none'), ('failure', 'workflow_dispatch', 'failed'),
                 ('success', 'workflow_dispatch', 'none'), ('cancelled', 'workflow_dispatch', 'failed')]
        for result, event, expected in cases:
            with self.subTest(result=result, event=event):
                self.assertEqual(f'action={expected}', self.decide(result, event, runs, python='exit 2')[0])

    def test_the_lookup_and_the_helper_are_time_bounded(self):
        _, calls = self.decide('failure', 'schedule', [{'id': 99, 'conclusion': 'success'}])
        bounds = {call.split()[2]: int(call.split()[1]) for call in calls if call.startswith('timeout ')}
        self.assertEqual({'gh': 30, 'python3': 60}, bounds)

    def test_a_hung_helper_or_lookup_does_not_hide_a_failure(self):
        runs = [{'id': 99, 'conclusion': 'failure'}]
        hung = 'case "$2" in python3|gh) exit 124;; esac\nshift; exec "$@"'
        for result, event, expected in (('failure', 'schedule', 'failed'), ('success', 'schedule', 'none'),
                                        ('cancelled', 'schedule', 'none'), ('cancelled', 'workflow_dispatch', 'failed')):
            with self.subTest(result=result, event=event):
                self.assertEqual(f'action={expected}', self.decide(result, event, runs, timeout=hung)[0])
        # A hung run-list lookup alone still lets the helper decide, failing open.
        lookup_hung = 'case "$2" in gh) exit 124;; esac\nshift; exec "$@"'
        self.assertEqual('action=failed', self.decide('failure', 'schedule', runs, timeout=lookup_hung)[0])

    def test_a_successful_rerun_of_a_failed_run_posts_a_recovery(self):
        # The rerun is in progress, so the completed list ends at the healthy run before it.
        runs = [{'id': 99, 'conclusion': 'success'}]
        action, calls = self.decide('success', 'schedule', runs, attempt=2, previous='failure')
        self.assertEqual('action=recovered', action)
        self.assertIn('gh api repos/jaegertracing/jaeger-demo/actions/runs/100/attempts/1', calls)
        # The run now counts as healthy, so the next scheduled run stays silent.
        runs = [{'id': 100, 'conclusion': 'success'}, {'id': 99, 'conclusion': 'success'}]
        self.assertEqual('action=none', self.decide('success', 'schedule', runs)[0])

    def test_a_failed_rerun_of_a_failed_run_stays_silent(self):
        runs = [{'id': 99, 'conclusion': 'success'}]
        self.assertEqual('action=none', self.decide('failure', 'schedule', runs, attempt=2, previous='failure')[0])

    def test_the_first_attempt_reads_no_earlier_attempt(self):
        _, calls = self.decide('success', 'schedule', [{'id': 99, 'conclusion': 'failure'}])
        self.assertEqual(1, len([c for c in calls if c.startswith('gh ')]))
        self.assertNotIn('/attempts/', ' '.join(calls))

    def test_an_unreadable_earlier_attempt_does_not_hide_a_failure(self):
        runs = [{'id': 99, 'conclusion': 'success'}]
        self.assertEqual('action=failed', self.decide('failure', 'schedule', runs, attempt=2, previous=None)[0])
        self.assertEqual('action=none', self.decide('success', 'schedule', runs, attempt=2, previous=None)[0])

    def test_a_failed_slack_post_cannot_change_the_run_result(self):
        self.assertIs(True, self.FAILED['continue-on-error'])
        self.assertIs(True, self.RECOVERED['continue-on-error'])
        self.assertNotIn('continue-on-error', self.DECIDE)
        self.assertNotIn('continue-on-error', STEPS['Check the OCI demo'])
        self.assertNotIn('continue-on-error', STEPS['Check the OpenTelemetry demo'])


if __name__ == '__main__':
    unittest.main()
