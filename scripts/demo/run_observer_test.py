# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Keep the run observer limited to runs that ended before a job started."""

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / '.github/workflows'
SOURCE = (WORKFLOWS / 'demo-run-observer.yml').read_text()
OBSERVER = yaml.safe_load(SOURCE)
DEPLOY = (WORKFLOWS / 'ci-deploy-demo.yml').read_text()
SMOKE = (WORKFLOWS / 'demo-smoke.yml').read_text()
JOB = OBSERVER['jobs']['observe']
LOOKUP, SLACK = JOB['steps']
UNSUCCESSFUL = ['cancelled', 'failure', 'timed_out', 'startup_failure']


def workflow_name(text):
    return re.search(r'(?m)^name: (.+)$', text)[1]


def pinned(text, action):
    return set(re.findall(re.escape(action) + r'@([0-9a-f]{40})', text))


def job(steps=None, conclusion='cancelled', **extra):
    """A job as the jobs API returns it. started_at is set even when nothing ran."""
    data = {'name': 'job', 'status': 'completed', 'conclusion': conclusion,
            'started_at': '2026-11-21T13:00:00Z', **extra}
    if steps is not None:
        data['steps'] = [{'name': f'step {n}', 'conclusion': 'success'} for n in range(steps)]
    return data


class RunObserverTests(unittest.TestCase):
    def run_lookup(self, response=None, gh_status=0):
        """Run the lookup step with a stub gh and return (result, notify output, gh arguments)."""
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            (temp / 'response.json').write_text(response if response is not None else '')
            stub = temp / 'bin' / 'gh'
            stub.parent.mkdir()
            stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$GH_CALLS"\n'
                            f'cat "{temp / "response.json"}"\nexit {gh_status}\n')
            stub.chmod(0o755)
            output = temp / 'output'
            output.write_text('')
            env = {'PATH': f'{stub.parent}:{os.environ["PATH"]}', 'GH_CALLS': str(temp / 'calls'),
                   'GITHUB_OUTPUT': str(output), 'RUNNER_TEMP': str(temp),
                   'GITHUB_REPOSITORY': 'jaegertracing/jaeger-demo', 'RUN_ID': '123', 'RUN_ATTEMPT': '2'}
            result = subprocess.run(['bash', '-eo', 'pipefail', '-c', LOOKUP['run']], env=env,
                                    capture_output=True, text=True, check=False)
            calls = (temp / 'calls').read_text().splitlines()
            return result, output.read_text(), calls

    def test_observes_the_deploy_and_smoke_workflows_by_their_current_names(self):
        # PyYAML reads the key "on" as the boolean True.
        trigger = OBSERVER[True]
        self.assertEqual(['workflow_run'], list(trigger))
        self.assertEqual([workflow_name(DEPLOY), workflow_name(SMOKE)], trigger['workflow_run']['workflows'])
        self.assertEqual(['completed'], trigger['workflow_run']['types'])
        self.assertNotIn(workflow_name(SOURCE), trigger['workflow_run']['workflows'])

    def test_runs_only_for_unsuccessful_conclusions_in_this_repository(self):
        condition = JOB['if']
        self.assertEqual(1, condition.count("github.repository == 'jaegertracing/jaeger-demo'"))
        listed = re.search(r"contains\(fromJSON\('(\[[^']+\])'\), github\.event\.workflow_run\.conclusion\)", condition)
        self.assertEqual(UNSUCCESSFUL, json.loads(listed[1]))
        expression = condition.replace(listed[0], 'conclusion in listed').replace('github.repository', 'repository')
        expression = expression.replace('&&', 'and')
        self.assertNotRegex(expression, r'\|\||github\.|\$')
        cases = {conclusion: True for conclusion in UNSUCCESSFUL}
        cases.update({conclusion: False for conclusion in ('success', 'skipped', 'neutral', 'action_required', 'stale', '')})
        for repository in ('jaegertracing/jaeger-demo', 'someone/jaeger-demo'):
            for conclusion, expected in cases.items():
                with self.subTest(repository=repository, conclusion=conclusion):
                    names = {'conclusion': conclusion, 'listed': json.loads(listed[1]), 'repository': repository}
                    self.assertEqual(expected and repository == 'jaegertracing/jaeger-demo',
                                     eval(expression, {'__builtins__': {}}, names))

    def test_checks_out_no_code_and_uses_only_the_pinned_slack_action(self):
        self.assertNotIn('checkout', SOURCE)
        self.assertEqual(2, len(JOB['steps']))
        self.assertNotIn('uses', LOOKUP)
        self.assertNotRegex(LOOKUP['run'], r'\bgit\b|\bcurl\b|\bpython')
        slack = pinned(SOURCE, 'rtCamp/action-slack-notify')
        self.assertTrue(slack)
        self.assertEqual(slack, pinned(DEPLOY, 'rtCamp/action-slack-notify'))
        self.assertEqual(1, len(re.findall(r'(?m)^\s*uses:', SOURCE)))

    def test_token_can_only_read_actions_and_the_webhook_is_the_only_secret(self):
        self.assertEqual({}, OBSERVER['permissions'])
        self.assertEqual({'actions': 'read'}, JOB['permissions'])
        self.assertNotRegex(SOURCE, r'(?m)^\s*[a-z-]+: write\b')
        self.assertEqual({'SLACK_WEBHOOK_URL'}, set(re.findall(r'secrets\.(\w+)', SOURCE)))
        self.assertNotIn('environment', JOB)
        self.assertEqual('${{ github.token }}', LOOKUP['env']['GH_TOKEN'])

    def test_job_is_bounded_and_the_script_takes_event_data_from_the_environment(self):
        self.assertIsInstance(JOB['timeout-minutes'], int)
        self.assertLessEqual(JOB['timeout-minutes'], 10)
        self.assertNotIn('${{', LOOKUP['run'])
        self.assertEqual('${{ github.event.workflow_run.id }}', LOOKUP['env']['RUN_ID'])
        self.assertEqual('${{ github.event.workflow_run.run_attempt }}', LOOKUP['env']['RUN_ATTEMPT'])

    def test_a_run_without_a_started_job_is_reported(self):
        cases = {
            'no jobs at all': {'total_count': 0, 'jobs': []},
            'cancelled while pending': {'jobs': [job(steps=0)]},
            'steps key absent': {'jobs': [job()]},
            'every job skipped': {'jobs': [job(steps=0, conclusion='skipped'), job(steps=0, conclusion='skipped')]},
        }
        for name, response in cases.items():
            with self.subTest(case=name):
                result, output, calls = self.run_lookup(json.dumps(response))
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual('notify=true\n', output)
                self.assertEqual(['api repos/jaegertracing/jaeger-demo/actions/runs/123/attempts/2/jobs?per_page=100'], calls)

    def test_a_run_whose_job_started_reports_itself_and_is_not_reported_again(self):
        cases = {
            'one job ran': {'jobs': [job(steps=14, conclusion='failure')]},
            'one ran and one was skipped': {'jobs': [job(steps=0, conclusion='skipped'), job(steps=3)]},
            'cancelled during setup': {'jobs': [job(steps=1)]},
        }
        for name, response in cases.items():
            with self.subTest(case=name):
                result, output, _ = self.run_lookup(json.dumps(response))
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual('notify=false\n', output)

    def test_an_unreadable_job_list_is_reported_with_a_warning(self):
        cases = {
            'request failed': ('', 1),
            'empty reply': ('', 0),
            'not JSON': ('<html>', 0),
            'no jobs key': ('{"message": "Not Found"}', 0),
            'failed after a valid reply': (json.dumps({'jobs': [job(steps=5)]}), 1),
        }
        for name, (response, status) in cases.items():
            with self.subTest(case=name):
                result, output, _ = self.run_lookup(response, status)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual('notify=true\n', output)
                self.assertIn('::warning::Could not read the jobs of run 123', result.stdout)

    def test_slack_step_follows_the_lookup_and_fails_open(self):
        condition = SLACK['if']
        self.assertEqual("${{ !cancelled() && steps.observed.outputs.notify != 'false' }}", condition)
        self.assertEqual('observed', LOOKUP['id'])
        self.assertNotIn('if', LOOKUP)
        env = SLACK['env']
        self.assertEqual('${{ secrets.SLACK_WEBHOOK_URL }}', env['SLACK_WEBHOOK'])
        self.assertEqual("#jaeger-operations", env['SLACK_CHANNEL'])
        self.assertEqual('true', env['MSG_MINIMAL'])
        for field in ('name', 'id', 'conclusion', 'event', 'actor.login', 'html_url'):
            self.assertIn('${{ github.event.workflow_run.' + field + ' }}', env['SLACK_MESSAGE'])


if __name__ == '__main__':
    unittest.main()
