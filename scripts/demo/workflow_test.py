# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Exercise the repository boundary before any deployment credentials are used."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / '.github/workflows'
CORE_SHA = '1' * 40
CONFIG_SHA = '2' * 40


def step_script(name):
    workflow = (WORKFLOWS / 'ci-deploy-demo.yml').read_text()
    step = workflow.split('    - name: ' + name + '\n', 1)[1].split('\n    - name:', 1)[0]
    lines = step.split('      run: |\n', 1)[1].splitlines()
    return '\n'.join(line[8:] for line in lines if line.startswith('        '))


class WorkflowTests(unittest.TestCase):
    def test_production_and_chart_tests_use_the_same_pinned_helm_version(self):
        versions = []
        for filename in ('ci-deploy-demo.yml', 'ci.yml'):
            with self.subTest(workflow=filename):
                source = (WORKFLOWS / filename).read_text()
                setup = source.split('uses: azure/setup-helm@', 1)[1].split('\n    - name:', 1)[0]
                version = re.search(r'^        version: (v\d+\.\d+\.\d+)$', setup, re.MULTILINE)
                self.assertIsNotNone(version, 'Helm must use an exact release, not the action default')
                versions.append(version[1])
        self.assertEqual(2, len(versions))
        self.assertEqual(versions[0], versions[1])

    def test_oci_cli_install_is_constrained_and_verified_before_reconciliation(self):
        source = (WORKFLOWS / 'ci-deploy-demo.yml').read_text()
        configure = source.split('    - name: Configure kubectl with OKE\n', 1)[1].split('\n    - name:', 1)[0]
        self.assertIn('        PIP_CONSTRAINT: ${{ github.workspace }}/scripts/demo/oci-cli-constraints.txt\n', configure)
        check = source.index('    - name: Verify OCI CLI version\n      run: python scripts/demo/verify_oci_cli.py\n')
        self.assertLess(source.index('    - name: Configure kubectl with OKE\n'), check)
        self.assertLess(check, source.index('    - name: Reconcile demo components\n'))

    def test_maintenance_workflows_are_manual_main_only_and_environment_scoped(self):
        for filename in ('ci-opensearch-recovery.yml', 'ci-opensearch-volume-expand.yml'):
            with self.subTest(workflow=filename):
                source = (WORKFLOWS / filename).read_text()
                events = source.split('\non:\n', 1)[1].split('\npermissions:', 1)[0]
                self.assertEqual(['workflow_dispatch'], re.findall(r'^  ([a-z_]+):', events, re.MULTILINE))
                self.assertIn("    if: github.ref == 'refs/heads/main'\n    environment: demo-apply", source)
                self.assertIn('  group: jaeger-otel-demo-opensearch-recovery\n  cancel-in-progress: false', source)
                self.assertNotIn('pull_request_target', source)

    def test_daily_activation_and_manual_jobs_use_separate_environments(self):
        source = (WORKFLOWS / 'ci-deploy-demo.yml').read_text()
        events = source.split('\non:\n', 1)[1].split('\npermissions:', 1)[0]
        self.assertEqual(['schedule', 'workflow_dispatch'], re.findall(r'^  ([a-z_]+):', events, re.MULTILINE))
        self.assertIn("  schedule:\n    - cron: '0 13 * * *'\n", events)
        manual, request = source.split('\n  request-daily:\n', 1)
        self.assertIn("    environment: demo-apply\n", manual)
        self.assertIn("    environment: demo-scheduled\n", request)
        self.assertIn("scheduled_request.py request", request)
        for dangerous in ("azure/setup-helm", "scripts/demo/deploy.py", "manual-acquire", "demo-apply"):
            self.assertNotIn(dangerous, request)
        self.assertIn("Send detailed Slack notification on failure", request)
        self.assertIn("env: *demo-failure-alert", request)
        for section in (manual, request):
            self.assertIn('    concurrency:\n      group: jaeger-otel-demo-opensearch-recovery\n      cancel-in-progress: false', section)
            condition = ' '.join(section.split('    if: >-\n', 1)[1].split('\n    environment:', 1)[0].split())
            for event in ('schedule', 'workflow_dispatch', 'push', 'pull_request_target'):
                for ref in ('refs/heads/main', 'refs/heads/topic', 'refs/tags/main'):
                    for switch in ('true', 'false', '', 'yes'):
                        with self.subTest(event=event, ref=ref, switch=switch):
                            expression = condition.replace('github.event_name', repr(event)).replace('github.ref', repr(ref))
                            expression = expression.replace('vars.DEMO_SCHEDULE_ENABLED', repr(switch))
                            expression = expression.replace('&&', 'and').replace('||', 'or')
                            intended = event == 'workflow_dispatch' if section == manual else event == 'schedule' and switch == 'true'
                            self.assertEqual(ref == 'refs/heads/main' and intended,
                                             eval(expression, {'__builtins__': {}}, {}))

    def test_all_manual_writers_use_the_shared_lock(self):
        for workflow, operation in [('ci-deploy-demo.yml','Reconcile demo components'),
                                   ('ci-opensearch-recovery.yml','Create and verify OpenSearch recovery point'),
                                   ('ci-opensearch-volume-expand.yml','Expand and verify the OpenSearch volume')]:
            source = (WORKFLOWS / workflow).read_text()
            self.assertLess(source.index('scheduled_request.py manual-acquire'),source.index('- name: '+operation))
            self.assertLess(source.index('- name: '+operation),source.index('scheduled_request.py manual-release'))
            self.assertIn("if: success() && steps.writer-lock.outputs.held == 'true'",source)

    def test_config_guard_accepts_only_matching_full_sha_or_no_override(self):
        script = step_script('Verify the reviewed configuration revision')
        for expected, status in ((CONFIG_SHA, 0), ('', 0), (CORE_SHA, 1), ('main', 1), (CONFIG_SHA[:7], 1)):
            with self.subTest(expected=expected):
                result = subprocess.run(['bash', '-eo', 'pipefail', '-c', script],
                                        env=dict(os.environ, EXPECTED_CONFIG_SHA=expected, GITHUB_SHA=CONFIG_SHA),
                                        text=True, capture_output=True)
                self.assertEqual(status, result.returncode, result.stderr)

    def resolve_core(self, output, status=0):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            git = directory / 'git'
            git.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$GIT_LOG"\nprintf "%s" "$GIT_OUTPUT"\nexit "$GIT_STATUS"\n')
            git.chmod(0o755)
            env_file = directory / 'env'
            summary = directory / 'summary'
            log = directory / 'git.log'
            result = subprocess.run(['bash', '-eo', 'pipefail', '-c', step_script('Resolve upstream Jaeger main')],
                                    env=dict(os.environ, PATH=temp + os.pathsep + os.environ['PATH'],
                                             GIT_OUTPUT=output, GIT_STATUS=str(status), GIT_LOG=str(log),
                                             GITHUB_ENV=str(env_file), GITHUB_STEP_SUMMARY=str(summary),
                                             GITHUB_SHA=CONFIG_SHA), text=True, capture_output=True)
            return result, env_file.read_text() if env_file.exists() else '', log.read_text()

    def test_upstream_main_comes_from_core_even_when_configuration_sha_differs(self):
        result, exports, invocation = self.resolve_core(CORE_SHA + '\trefs/heads/main\n')
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('MAIN_SHA=' + CORE_SHA + '\n', exports)
        self.assertEqual('ls-remote https://github.com/jaegertracing/jaeger.git refs/heads/main\n', invocation)
        self.assertNotIn(CONFIG_SHA, exports)

    def test_failed_empty_ambiguous_or_malformed_upstream_resolution_stops(self):
        for output, status in (('', 1), ('', 0), ('main\trefs/heads/main\n', 0),
                               ((CORE_SHA + '\trefs/heads/main\n') * 2, 0)):
            with self.subTest(output=output, git_status=status):
                result, exports, _ = self.resolve_core(output, status)
                self.assertNotEqual(0, result.returncode)
                self.assertEqual('', exports)

    def test_revision_checks_precede_access_and_image_defaults_exclude_config_sha(self):
        source = (WORKFLOWS / 'ci-deploy-demo.yml').read_text()
        access = source.index('    - name: Configure kubectl with OKE')
        self.assertLess(source.index('    - name: Verify the reviewed configuration revision'), access)
        self.assertLess(source.index('    - name: Resolve upstream Jaeger main'), access)
        self.assertIn('    - name: Resolve upstream Jaeger main\n      shell: bash\n', source)
        image_lines = [line for line in source.splitlines() if 'IMAGE_TAG:' in line]
        self.assertEqual(3, len(image_lines))
        self.assertTrue(all('github.sha' not in line for line in image_lines))
        self.assertNotIn('      MAIN_SHA: ${{ github.sha }}', source)
        self.assertIn('    - name: Configure kubectl with OKE\n      id: configure-oke\n', source)
        diagnostics = source.split('    - name: Collect OpenSearch recovery diagnostics\n', 1)[1].split('\n    - name:', 1)[0]
        self.assertIn("      if: failure() && github.event_name == 'workflow_dispatch' && steps.configure-oke.outcome == 'success' && steps.reconcile.outputs.recovery_started == 'true'\n", diagnostics)

    def test_restore_cleanup_runs_only_after_recovery_started(self):
        source = (WORKFLOWS / 'ci-deploy-demo.yml').read_text()
        self.assertIn('    - name: Reconcile demo components\n      id: reconcile\n', source)
        cleanup = source.split('    - name: Remove disposable restore pod\n', 1)[1].split('\n    - name:', 1)[0]
        condition = re.search(r'^      if: (.+)$', cleanup, re.MULTILINE)[1]
        for configure in ('success', 'failure', 'cancelled', 'skipped', ''):
            for reconcile in ('success', 'failure', 'cancelled', 'skipped', ''):
                for recovery in ('true', 'false', ''):
                    with self.subTest(configure=configure, reconcile=reconcile, recovery=recovery):
                        expression = condition.replace('always()', 'True')
                        expression = expression.replace('steps.configure-oke.outcome', repr(configure))
                        expression = expression.replace('steps.reconcile.outcome', repr(reconcile))
                        expression = expression.replace('steps.reconcile.outputs.recovery_started', repr(recovery))
                        expression = expression.replace('&&', 'and').replace('||', 'or')
                        for event in ('workflow_dispatch', 'schedule'):
                            result = eval(expression.replace('github.event_name', repr(event)), {'__builtins__': {}}, {})
                            self.assertEqual(event == 'workflow_dispatch' and configure == 'success' and recovery == 'true'
                                             and reconcile in ('success', 'failure', 'cancelled'), result)

    def test_recovery_input_stays_manual_and_policy_validates_before_cluster_access(self):
        source = (WORKFLOWS / 'ci-deploy-demo.yml').read_text()
        self.assertIn('      JAEGER_OTEL_DEMO_OPENSEARCH_RECOVERY: ${{ inputs.opensearch_recovery }}', source)
        self.assertIn('    - name: Validate OpenSearch recovery policy\n      run: |\n        python3 scripts/demo/recovery_policy.py', source)
        self.assertLess(source.index('    - name: Validate OpenSearch recovery policy'),
                        source.index('    - name: Configure kubectl with OKE'))

    def test_workflow_recovery_policy_matches_event_and_scope(self):
        script = step_script('Validate OpenSearch recovery policy')
        for event, ref, stack, mode, scope, requested, expected in (
                ('schedule', 'main', 'oci', 'upgrade', 'jaeger', '', 'preserve-pvc'),
                ('schedule', 'main', 'oci', 'upgrade', 'jaeger', 'required', None),
                ('schedule', 'main', 'otel', 'clean', 'opensearch', '', None),
                ('schedule', 'feature', 'otel', 'upgrade', 'opensearch', '', None),
                ('workflow_dispatch', 'main', 'otel', 'upgrade', 'opensearch', '', 'required'),
                ('workflow_dispatch', 'main', 'all', 'upgrade', 'all', 'required', 'required'),
                ('workflow_dispatch', 'main', 'otel', 'upgrade', 'opensearch', 'waive', 'waive'),
                ('workflow_dispatch', 'main', 'otel', 'upgrade', 'all', 'waive', None),
                ('workflow_dispatch', 'main', 'otel', 'upgrade', 'opensearch', 'preserve-pvc', None)):
            env = dict(os.environ, GITHUB_EVENT_NAME=event, GITHUB_REF='refs/heads/' + ref, JAEGER_DEMO_STACK=stack,
                       JAEGER_DEMO_DEPLOY_MODE=mode, JAEGER_OTEL_DEMO_DEPLOY_SCOPE=scope,
                       JAEGER_OTEL_DEMO_OPENSEARCH_RECOVERY=requested)
            with self.subTest(event=event, ref=ref, stack=stack, mode=mode, scope=scope, requested=requested):
                result = subprocess.run(['bash', '-e', '-c', script], cwd=ROOT, env=env, text=True, capture_output=True)
                self.assertEqual(expected is not None, result.returncode == 0, result.stderr)
                self.assertEqual(expected or '', result.stdout.strip())

    def test_reviewed_sha_is_shared_with_reconciler_and_child_scripts(self):
        source = (WORKFLOWS / 'ci-deploy-demo.yml').read_text()
        job = source.split('\n    steps:', 1)[0]
        self.assertIn('      EXPECTED_CONFIG_SHA: ${{ inputs.expected_config_sha }}\n', job)


if __name__ == '__main__':
    unittest.main()
