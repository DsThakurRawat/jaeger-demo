# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Exercise the repository boundary before any deployment credentials are used."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

import yaml


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

    def test_every_helm_install_uses_one_pinned_action_and_requires_the_checksum(self):
        steps = []
        for path in sorted(WORKFLOWS.glob('*.yml')):
            for job in (yaml.safe_load(path.read_text()).get('jobs') or {}).values():
                steps += [(path.name, step) for step in job.get('steps') or []
                          if str(step.get('uses', '')).startswith('azure/setup-helm@')]
        self.assertEqual(['ci-deploy-demo.yml', 'ci.yml'], [name for name, _ in steps])
        self.assertEqual(1, len({step['uses'] for _, step in steps}))
        for name, step in steps:
            with self.subTest(workflow=name):
                self.assertRegex(step['uses'], r'^azure/setup-helm@[0-9a-f]{40}$')
                # The action downloads Helm; without this it only warns when no checksum is published.
                self.assertEqual('required', step['with']['checksum-policy'])
                self.assertRegex(step['with']['version'], r'^v\d+\.\d+\.\d+$')

    def test_oci_cli_install_is_constrained_and_verified_before_reconciliation(self):
        source = (WORKFLOWS / 'ci-deploy-demo.yml').read_text()
        configure = source.split('    - name: Configure kubectl with OKE\n', 1)[1].split('\n    - name:', 1)[0]
        self.assertIn('        PIP_CONSTRAINT: ${{ github.workspace }}/scripts/demo/oci-cli-constraints.txt\n', configure)
        check = source.index('    - name: Verify OCI CLI version\n      run: python scripts/demo/verify_oci_cli.py\n')
        self.assertLess(source.index('    - name: Configure kubectl with OKE\n'), check)
        self.assertLess(check, source.index('    - name: Reconcile demo components\n'))

    def test_maintenance_workflows_constrain_and_verify_oci_cli_before_cluster_access(self):
        for filename, script in (('ci-opensearch-recovery.yml', '    - name: Create and verify OpenSearch recovery point\n'),
                                 ('ci-opensearch-volume-expand.yml', '    - name: Expand and verify the OpenSearch volume\n')):
            with self.subTest(workflow=filename):
                source = (WORKFLOWS / filename).read_text()
                configure_header = '    - name: Configure kubectl with OKE\n'
                configure = source.split(configure_header, 1)[1].split('\n    - name:', 1)[0]
                self.assertIn('      env:\n        PIP_CONSTRAINT: ${{ github.workspace }}/scripts/demo/oci-cli-constraints.txt\n', configure)
                order = [source.index(step) for step in (
                    '    - name: Checkout demo configuration\n', configure_header,
                    '    - name: Verify OCI CLI version\n      run: python scripts/demo/verify_oci_cli.py\n',
                    "    - name: Verify the deployment identity's access\n", script)]
                self.assertEqual(sorted(order), order)
                self.assertEqual(1, source.count('verify_oci_cli.py'))

    def test_recovery_diagnostics_and_cleanup_need_the_access_check(self):
        source = (WORKFLOWS / 'ci-opensearch-recovery.yml').read_text()
        for name, trigger in (('Collect failure diagnostics', 'failure()'), ('Remove disposable restore pod', 'always()')):
            step = source.split('    - name: %s\n' % name, 1)[1].split('\n    - name:', 1)[0]
            condition = re.search(r'^      if: (.+)$', step, re.MULTILINE)[1]
            # A rejected OCI CLI check skips the access check; kubectl must not run after it.
            self.assertNotIn('writer-lock', condition)
            for access in ('success', 'failure', 'cancelled', 'skipped'):
                for job_failed in (True, False):
                    with self.subTest(step=name, access=access, job_failed=job_failed):
                        expression = condition.replace(trigger, repr(job_failed if trigger == 'failure()' else True))
                        expression = expression.replace('steps.access.outcome', repr(access)).replace('&&', 'and')
                        expected = access == 'success' and (job_failed or trigger == 'always()')
                        self.assertEqual(expected, eval(expression, {'__builtins__': {}}, {}))

    def test_maintenance_workflows_are_manual_main_only_and_environment_scoped(self):
        for filename in ('ci-opensearch-recovery.yml', 'ci-opensearch-volume-expand.yml'):
            with self.subTest(workflow=filename):
                source = (WORKFLOWS / filename).read_text()
                events = source.split('\non:\n', 1)[1].split('\npermissions:', 1)[0]
                self.assertEqual(['workflow_dispatch'], re.findall(r'^  ([a-z_]+):', events, re.MULTILINE))
                self.assertIn("    if: github.ref == 'refs/heads/main'\n    environment: demo-apply", source)
                self.assertIn('  group: jaeger-otel-demo-opensearch-recovery\n  cancel-in-progress: false', source)
                self.assertNotIn('pull_request_target', source)

    def test_one_job_selects_its_environment_by_event(self):
        source = (WORKFLOWS / 'ci-deploy-demo.yml').read_text()
        events = source.split('\non:\n', 1)[1].split('\npermissions:', 1)[0]
        self.assertEqual(['schedule', 'workflow_dispatch'], re.findall(r'^  ([a-z_]+):', events, re.MULTILINE))
        self.assertIn("  schedule:\n    - cron: '0 13 * * *'\n", events)
        workflow = yaml.safe_load(source)
        self.assertEqual(['deploy'], list(workflow['jobs']))
        job = workflow['jobs']['deploy']
        self.assertEqual({'group': 'jaeger-otel-demo-opensearch-recovery', 'cancel-in-progress': False}, job['concurrency'])
        environment = re.fullmatch(r'\$\{\{ (.+) \}\}', job['environment'])[1]
        condition = ' '.join(job['if'].split())

        def evaluate(expression, event, ref, switch):
            expression = expression.replace('github.event_name', repr(event)).replace('github.ref', repr(ref))
            expression = expression.replace('vars.DEMO_SCHEDULE_ENABLED', repr(switch))
            return eval(expression.replace('&&', 'and').replace('||', 'or'), {'__builtins__': {}}, {})

        for event in ('schedule', 'workflow_dispatch', 'push', 'pull_request_target', 'workflow_run'):
            for ref in ('refs/heads/main', 'refs/heads/topic', 'refs/tags/main'):
                for switch in ('true', 'false', '', 'yes'):
                    with self.subTest(event=event, ref=ref, switch=switch):
                        intended = event == 'workflow_dispatch' or event == 'schedule' and switch == 'true'
                        self.assertEqual(ref == 'refs/heads/main' and intended, evaluate(condition, event, ref, switch))
                        # Only a scheduled run gets the environment with no reviewer.
                        self.assertEqual('demo-scheduled' if event == 'schedule' else 'demo-apply',
                                         evaluate(environment, event, ref, switch))
        # The access check asks for the role of the identity the environment holds.
        access = next(step for step in job['steps'] if step.get('id') == 'access')
        identity = re.fullmatch(r'\$\{\{ (.+) \}\}', access['env']['DEPLOYER_IDENTITY'])[1]
        for event in ('schedule', 'workflow_dispatch', 'push'):
            self.assertEqual('scheduled' if event == 'schedule' else 'manual', evaluate(identity, event, '', ''))
        self.assertEqual('python scripts/demo/deployer_access.py check --self --identity "$DEPLOYER_IDENTITY"', access['run'])
        # Scheduled runs take no manual input, so the recovery policy stays empty and resolves to preserve-pvc.
        self.assertEqual('${{ inputs.opensearch_recovery }}', job['env']['JAEGER_OTEL_DEMO_OPENSEARCH_RECOVERY'])

    def test_all_writers_share_one_concurrency_group_and_take_no_lock(self):
        for workflow, operation in [('ci-deploy-demo.yml','Reconcile demo components'),
                                   ('ci-opensearch-recovery.yml','Create and verify OpenSearch recovery point'),
                                   ('ci-opensearch-volume-expand.yml','Expand and verify the OpenSearch volume')]:
            with self.subTest(workflow=workflow):
                source = (WORKFLOWS / workflow).read_text()
                self.assertEqual(1, source.count('group: jaeger-otel-demo-opensearch-recovery\n'))
                self.assertEqual(1, source.count('cancel-in-progress: false\n'))
                for retired in ('scheduled_request.py', 'writer-lock', 'JAEGER_DEMO_LOCK_HELD', 'deployment lock'):
                    self.assertNotIn(retired, source)
                # The credential's access is checked before the operation that changes the cluster.
                check = source.index('deployer_access.py check --self --identity ')
                self.assertLess(source.index('verify_oci_cli.py'), check)
                self.assertLess(check, source.index('- name: ' + operation))
        for workflow in ('ci-opensearch-recovery.yml', 'ci-opensearch-volume-expand.yml'):
            self.assertIn('deployer_access.py check --self --identity manual\n', (WORKFLOWS / workflow).read_text())

    def test_only_the_writer_workflows_use_a_deployment_environment(self):
        writers = {'ci-deploy-demo.yml', 'ci-opensearch-recovery.yml', 'ci-opensearch-volume-expand.yml'}
        for path in sorted(WORKFLOWS.glob('*.yml')):
            source = path.read_text()
            with self.subTest(workflow=path.name):
                self.assertNotIn('pull_request_target', source)
                jobs = yaml.safe_load(source).get('jobs') or {}
                environments = [job['environment'] for job in jobs.values() if 'environment' in job]
                if path.name not in writers:
                    self.assertEqual([], environments)
                    self.assertNotIn('OCI_CLI_KEY_CONTENT', source)
                    self.assertNotIn('OKE_CLUSTER_OCID', source)
                elif path.name != 'ci-deploy-demo.yml':
                    self.assertEqual(['demo-apply'], environments)

    def scheduled_rerun(self, event, attempt, remote, status=0):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            git = directory / 'git'
            git.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$GIT_LOG"\nprintf "%s" "$GIT_OUTPUT"\nexit "$GIT_STATUS"\n')
            git.chmod(0o755)
            log = directory / 'git.log'
            result = subprocess.run(['bash', '-eo', 'pipefail', '-c', step_script('Verify the reviewed configuration revision')],
                                    env=dict(os.environ, PATH=temp + os.pathsep + os.environ['PATH'], GIT_OUTPUT=remote,
                                             GIT_STATUS=str(status), GIT_LOG=str(log), EXPECTED_CONFIG_SHA='',
                                             GITHUB_SHA=CONFIG_SHA, GITHUB_EVENT_NAME=event, GITHUB_RUN_ATTEMPT=attempt,
                                             GITHUB_REPOSITORY='jaegertracing/jaeger-demo'), text=True, capture_output=True)
            return result, log.read_text() if log.exists() else ''

    def test_scheduled_rerun_requires_current_main(self):
        current, moved = CONFIG_SHA + '\trefs/heads/main\n', CORE_SHA + '\trefs/heads/main\n'
        # A first attempt and every manual run are decided elsewhere and never ask the remote.
        for event, attempt in (('schedule', '1'), ('workflow_dispatch', '1'), ('workflow_dispatch', '3')):
            with self.subTest(event=event, attempt=attempt):
                result, invocation = self.scheduled_rerun(event, attempt, moved)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual('', invocation)
        result, invocation = self.scheduled_rerun('schedule', '2', current)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('ls-remote https://github.com/jaegertracing/jaeger-demo.git refs/heads/main\n', invocation)
        for remote, status in ((moved, 0), ('', 0), ('', 1), (current * 2, 0)):
            with self.subTest(remote=remote, status=status):
                result, _ = self.scheduled_rerun('schedule', '2', remote, status)
                self.assertNotEqual(0, result.returncode)
        self.assertIn('must be at the current main commit', self.scheduled_rerun('schedule', '2', moved)[0].stderr)

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

    def test_ci_jobs_have_timeouts(self):
        jobs = yaml.safe_load((WORKFLOWS / 'ci.yml').read_text())['jobs']
        self.assertTrue(jobs)
        for name, job in jobs.items():
            with self.subTest(job=name):
                self.assertIsInstance(job.get('timeout-minutes'), int)

    def test_ci_cancels_only_superseded_pull_request_runs(self):
        concurrency = yaml.safe_load((WORKFLOWS / 'ci.yml').read_text())['concurrency']
        self.assertIn('github.event.pull_request.number || github.sha', concurrency['group'])
        self.assertEqual("${{ github.event_name == 'pull_request' }}", concurrency['cancel-in-progress'])

    def test_ci_no_longer_flags_guarded_changes(self):
        # No executor image has to match main once the schedule deploys directly.
        source = (WORKFLOWS / 'ci.yml').read_text()
        self.assertNotIn('guarded_changes.py', source)
        self.assertNotIn('executor image', source)

    def test_no_workflow_builds_releases_or_rehearses_the_retired_executor(self):
        for name in ('executor-ci.yml', 'release-executor.yml'):
            self.assertFalse((WORKFLOWS / name).exists(), name)
        self.assertFalse((ROOT / 'ops/executor').exists())
        for path in sorted(WORKFLOWS.glob('*.yml')):
            source = path.read_text()
            for retired in ('ops/executor', 'scheduled_adapter', 'scheduled_inputs', 'rehearse-executor',
                            'executor-runtime', 'access_matrix', 'helm-history'):
                with self.subTest(workflow=path.name, retired=retired):
                    self.assertNotIn(retired, source)

    def test_adoption_job_checks_the_chart_object_inventory_against_the_pinned_charts(self):
        job = yaml.safe_load((WORKFLOWS / 'ci.yml').read_text())['jobs']['adoption']
        runs = [step['run'] for step in job['steps'] if 'run' in step]
        self.assertIn('python3 scripts/demo/helmfile_tools.py run -- python3 scripts/demo/chart_objects.py --check', runs)

    def test_demo_tests_job_checks_the_chart_archive_lock_against_the_publishers(self):
        jobs = yaml.safe_load((WORKFLOWS / 'ci.yml').read_text())['jobs']
        job, = [job for job in jobs.values() if job.get('name') == 'Demo tests']
        steps = [step for step in job['steps'] if step.get('run') == 'python3 scripts/utils/update-chart-archives.py --check']
        self.assertEqual(1, len(steps))
        self.assertNotIn('if', steps[0])
        self.assertNotIn('continue-on-error', steps[0])

    def test_renovate_job_uses_the_discovery_make_target(self):
        job = yaml.safe_load((WORKFLOWS / 'ci.yml').read_text())['jobs']['renovate']
        self.assertEqual(['make renovate-discovery'], [step['run'] for step in job['steps'] if 'run' in step])
        script = (ROOT / 'scripts/utils/renovate-discovery.sh').read_text()
        self.assertIn('RENOVATE_ENABLED_MANAGERS=custom.regex,kubernetes,helm-values,pip_requirements,helmfile ', script)
        self.assertIn('python3 scripts/demo/check-renovate-extraction.py', script)


    def test_unsuccessful_deployments_notify_and_reconcile_stops_inside_the_job_limit(self):
        source = (WORKFLOWS / 'ci-deploy-demo.yml').read_text()
        header = '    - name: Send detailed Slack notification on failure\n'
        notify, = source.split(header)[1:]
        # The one job serves both triggers and ends with the notification.
        self.assertNotIn('\n    - name:', notify)
        self.assertNotIn('\n  request-daily:', source)
        self.assertIn('*Status:* ${{ job.status }}', notify)
        self.assertIn('*Trigger:* ${{ github.event_name }}', notify)
        for notify in (notify,):
            condition = re.search(r'^      if: (.+)$', notify, re.MULTILINE)[1]
            for status in ('success', 'failure', 'cancelled'):
                with self.subTest(status=status):
                    expression = condition.replace('failure()', repr(status == 'failure'))
                    expression = expression.replace('cancelled()', repr(status == 'cancelled')).replace('||', 'or')
                    self.assertEqual(status != 'success', eval(expression, {'__builtins__': {}}, {}))

        job_limit = int(re.search(r'^    timeout-minutes: (\d+)$', source, re.MULTILINE)[1])
        reconcile = source.split('    - name: Reconcile demo components\n', 1)[1].split('\n    - name:', 1)[0]
        step_limit = int(re.search(r'^      timeout-minutes: (\d+)$', reconcile, re.MULTILINE)[1])
        # Reconcile keeps the 300 minutes the job allowed before the step had a limit
        # of its own. Setup and the later steps get the remaining 30 minutes.
        self.assertGreaterEqual(step_limit, 300)
        self.assertGreaterEqual(job_limit - step_limit, 30)
        self.assertLess(job_limit, 360)
        cleanup = source.split('    - name: Remove disposable restore pod\n', 1)[1].split('\n    - name:', 1)[0]
        self.assertIn('\n      timeout-minutes: 2\n', cleanup)
        diagnostics = source.split('    - name: Collect OpenSearch recovery diagnostics\n', 1)[1].split('\n    - name:', 1)[0]
        self.assertIn('\n      timeout-minutes: 5\n', diagnostics)
        self.assertEqual(3, diagnostics.count(' --request-timeout=30s || true'))
        self.assertIn(' --request-timeout=30s ', cleanup)


if __name__ == '__main__':
    unittest.main()
