# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Keep the scheduled public smoke check credential-free and bounded."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / '.github/workflows/demo-smoke.yml').read_text()
CI = (ROOT / '.github/workflows/ci.yml').read_text()
DEPLOY = (ROOT / '.github/workflows/ci-deploy-demo.yml').read_text()


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

    def test_notifies_on_failure_or_cancellation_with_only_the_webhook_secret(self):
        self.assertIn('if: failure() || cancelled()', WORKFLOW)
        self.assertEqual(set(re.findall(r'secrets\.(\w+)', WORKFLOW)), {'SLACK_WEBHOOK_URL'})


if __name__ == '__main__':
    unittest.main()
