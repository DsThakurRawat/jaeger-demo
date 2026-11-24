# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Exercise the repository boundary before any deployment credentials are used."""

import copy
import os
from pathlib import Path
import re
import subprocess
import sys
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


OCI_ENV = {name: '${{ secrets.%s }}' % name for name in (
    'OCI_CLI_USER', 'OCI_CLI_TENANCY', 'OCI_CLI_FINGERPRINT', 'OCI_CLI_KEY_CONTENT', 'OCI_CLI_REGION')}
KUBECTL_CONFIGURATION = re.compile(r'oracle-actions/configure-kubectl-oke@[0-9a-f]{40}')
SLACK_NOTIFICATION = re.compile(r'rtCamp/action-slack-notify@[0-9a-f]{40}')
EXPRESSION = re.compile(r'\$\{\{(.*?)\}\}', re.DOTALL)
USES_SECRETS = re.compile(r'\bsecrets\b', re.IGNORECASE)
WRITERS = ('ci-deploy-demo.yml', 'ci-opensearch-reset.yml', 'ci-opensearch-volume-expand.yml')
PIP_CONSTRAINT = '${{ github.workspace }}/scripts/demo/oci-cli-constraints.txt'
# Both constructs hand over every secret without naming one. Expression functions
# ignore case, and so does this search.
WHOLE_SECRETS = re.compile(r'tojson\(\s*secrets\s*\)|secrets\s*:\s*inherit', re.IGNORECASE)
HASHED_INSTALL = ('python -m pip install --require-hashes --only-binary :all: --no-binary PyYAML '
                  '-r scripts/demo/oci-cli-hashes.txt')


def secret_references(node, path=()):
    """Yield (path, value) for every string whose expression reads the secrets context, in any form."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from secret_references(value, path + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from secret_references(value, path + (index,))
    elif isinstance(node, str) and any(USES_SECRETS.search(expression) for expression in EXPRESSION.findall(node)):
        yield path, node


def allowed_secret_reference(step, place, key, value):
    """The only secret references the workflows need. A new one needs a reason here."""
    uses = step.get('uses', '')
    if place == 'env' and OCI_ENV.get(key) == value:
        return True  # the five OCI secrets, which the checks below confine to the steps that may hold them
    if place == 'with' and key == 'cluster' and value == '${{ secrets.OKE_CLUSTER_OCID }}':
        return bool(KUBECTL_CONFIGURATION.fullmatch(uses))
    if place == 'env' and key == 'SLACK_WEBHOOK' and value == '${{ secrets.SLACK_WEBHOOK_URL }}':
        return bool(SLACK_NOTIFICATION.fullmatch(uses))
    return False


def oci_credential_violations(workflow):
    """List each place a parsed workflow passes an OCI secret other than the env of a step that may hold it.

    Searching for the OCI names alone misses a name built at run time, such as
    secrets[format('{0}_CLI_KEY_CONTENT', 'OCI')], so every read of the secrets
    context must also be one that allowed_secret_reference names.
    """
    def mentions(value):
        # GitHub resolves secret names without regard to case, so secrets.oci_cli_key_content is the key too.
        return 'OCI_CLI_' in str(value).upper()

    found = []
    jobs = workflow.get('jobs') or {}
    for path, value in secret_references(workflow):
        is_step_value = len(path) == 6 and path[0] == 'jobs' and path[2] == 'steps' and path[4] in ('env', 'with')
        step = jobs[path[1]]['steps'][path[3]] if is_step_value else {}
        if not (is_step_value and allowed_secret_reference(step, path[4], path[5], value)):
            found.append('/'.join(str(part) for part in path) + ': a secret reference outside the allowed places')
    if mentions({key: value for key, value in workflow.items() if key != 'jobs'}):
        found.append('workflow level')
    for job_name, job in (workflow.get('jobs') or {}).items():
        if mentions({key: value for key, value in job.items() if key != 'steps'}):
            found.append(job_name + ': job level')
        for index, step in enumerate(job.get('steps') or []):
            label = '%s: step %s' % (job_name, step.get('name', index))
            if mentions({key: value for key, value in step.items() if key != 'env'}):
                found.append(label + ': outside the step env')
            held = {key: value for key, value in (step.get('env') or {}).items() if mentions((key, value))}
            if held and held != OCI_ENV:
                found.append(label + ': not exactly the five secrets under their own names')
            if held and 'uses' in step and not KUBECTL_CONFIGURATION.fullmatch(step['uses']):
                found.append(label + ': an action other than the pinned kubectl configuration')
    return found


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
        for filename, script in (('ci-opensearch-reset.yml', '    - name: List or reset the Jaeger trace indices\n'),
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

    def test_maintenance_workflows_are_manual_main_only_and_environment_scoped(self):
        for filename in ('ci-opensearch-reset.yml', 'ci-opensearch-volume-expand.yml'):
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
        # The guard runs under the shell the tests use, with pipefail.
        guard = next(step for step in job['steps'] if step['name'] == 'Verify the reviewed configuration revision')
        self.assertEqual('bash', guard['shell'])

    def test_all_writers_share_one_concurrency_group_and_take_no_lock(self):
        for workflow, operation in [('ci-deploy-demo.yml','Reconcile demo components'),
                                   ('ci-opensearch-reset.yml','List or reset the Jaeger trace indices'),
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
                # A hung API server must not hold the group for the whole job limit.
                jobs = yaml.safe_load(source)['jobs']
                access, = [step for job in jobs.values() for step in job['steps'] if step.get('id') == 'access']
                self.assertLessEqual(access['timeout-minutes'], 15)
        for workflow in ('ci-opensearch-reset.yml', 'ci-opensearch-volume-expand.yml'):
            self.assertIn('deployer_access.py check --self --identity manual\n', (WORKFLOWS / workflow).read_text())

    def test_the_writer_group_keeps_one_waiting_run_and_never_cancels_a_running_one(self):
        # The chosen setting (#102): GitHub's default of one waiting run per group,
        # which a newer waiting run replaces. `queue: max` would keep every waiting
        # run, but no released actionlint accepts the key
        # (https://github.com/rhysd/actionlint/issues/657). docs/releases.md states
        # the operator action. Change every workflow in the group and this test together.
        group = 'jaeger-otel-demo-opensearch-recovery'
        chosen = {'group': group, 'cancel-in-progress': False}
        declared = {}
        for path in sorted(WORKFLOWS.glob('*.yml')):
            workflow = yaml.safe_load(path.read_text())
            jobs = workflow.get('jobs') or {}
            settings = [('workflow', workflow.get('concurrency'))]
            settings += [('job ' + name, job.get('concurrency')) for name, job in jobs.items()]
            for level, setting in settings:
                name = setting.get('group') if isinstance(setting, dict) else setting
                # GitHub compares group names without regard to case.
                if str(name or '').lower() == group:
                    declared.setdefault(path.name, {})[level] = setting
            levels = declared.get(path.name, {})
            if levels and 'workflow' not in levels:
                # Without a workflow-level setting, each job that holds deployment credentials needs its own.
                for name, job in jobs.items():
                    with self.subTest(workflow=path.name, job=name):
                        self.assertTrue('environment' not in job or 'job ' + name in levels)
        self.assertEqual({'ci-deploy-demo.yml': {'job deploy': chosen},
                          'ci-opensearch-reset.yml': {'workflow': chosen},
                          'ci-opensearch-volume-expand.yml': {'workflow': chosen}}, declared)

    def test_only_the_writer_workflows_use_a_deployment_environment(self):
        writers = {'ci-deploy-demo.yml', 'ci-opensearch-reset.yml', 'ci-opensearch-volume-expand.yml'}
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

    def test_oci_credentials_reach_only_the_steps_that_use_them(self):
        # The kubeconfig runs the OCI CLI for a token, and the CLI reads the API key
        # from the environment, so each step that calls the cluster needs the five
        # secrets. No other step, and no action but the kubectl configuration, gets them.
        receiving = {
            'ci-deploy-demo.yml': [
                'Configure kubectl with OKE', "Verify the deployment identity's access", 'Reconcile demo components'],
            'ci-opensearch-reset.yml': [
                'Configure kubectl with OKE', "Verify the deployment identity's access",
                'List or reset the Jaeger trace indices'],
            'ci-opensearch-volume-expand.yml': [
                'Configure kubectl with OKE', "Verify the deployment identity's access",
                'Expand and verify the OpenSearch volume'],
        }
        self.assertEqual(sorted(WRITERS), sorted(receiving))
        for path in sorted(WORKFLOWS.glob('*.yml')):
            with self.subTest(workflow=path.name):
                source = path.read_text()
                workflow = yaml.safe_load(source)
                self.assertEqual([], oci_credential_violations(workflow))
                self.assertIsNone(WHOLE_SECRETS.search(source))
                steps = [step for job in workflow['jobs'].values() for step in job.get('steps') or []]
                holders = [step for step in steps if 'OCI_CLI_KEY_CONTENT' in (step.get('env') or {})]
                self.assertEqual(receiving.get(path.name, []), [step['name'] for step in holders])
                self.assertEqual(['oracle-actions/configure-kubectl-oke'] if holders else [],
                                 [step['uses'].split('@')[0] for step in holders if 'uses' in step])
                if path.name in receiving:
                    for step in steps:
                        if re.search(r'\b(kubectl|helm)\b', step.get('run', '')):
                            self.assertIn(step, holders, step['name'])

    def test_oci_credential_check_reports_each_way_a_secret_can_spread(self):
        workflow = yaml.safe_load((WORKFLOWS / 'ci-deploy-demo.yml').read_text())
        self.assertEqual([], oci_credential_violations(workflow))
        key = OCI_ENV['OCI_CLI_KEY_CONTENT']

        def step(candidate, name):
            return next(step for step in candidate['jobs']['deploy']['steps'] if step['name'] == name)

        def job_env(candidate):
            candidate['jobs']['deploy']['env'].update(OCI_ENV)

        def job_env_under_another_name(candidate):
            candidate['jobs']['deploy']['env']['SIGNING_KEY'] = key

        def workflow_env(candidate):
            candidate['env'] = dict(OCI_ENV)

        def job_container(candidate):
            candidate['jobs']['deploy']['container'] = {'image': 'example', 'env': dict(OCI_ENV)}

        def notification_action(candidate):
            step(candidate, 'Send detailed Slack notification on failure')['env'].update(OCI_ENV)

        def helm_action(candidate):
            step(candidate, 'Install Helm')['env'] = dict(OCI_ENV)

        def checkout_input(candidate):
            step(candidate, 'Checkout demo configuration')['with']['token'] = key

        def unpinned_configuration(candidate):
            step(candidate, 'Configure kubectl with OKE')['uses'] = 'oracle-actions/configure-kubectl-oke@v1'

        def script_interpolation(candidate):
            step(candidate, 'Verify OCI CLI version')['run'] += ' "${{ secrets.OCI_CLI_USER }}"'

        def renamed_variable(candidate):
            step(candidate, 'Verify OCI CLI version')['env'] = {'SIGNING_KEY': key}

        def partial_block(candidate):
            del step(candidate, 'Reconcile demo components')['env']['OCI_CLI_REGION']

        def crossed_names(candidate):
            step(candidate, 'Reconcile demo components')['env']['OCI_CLI_USER'] = OCI_ENV['OCI_CLI_TENANCY']

        # Secret names resolve without regard to case.
        def lowercase_job_env(candidate):
            candidate['jobs']['deploy']['env']['SIGNING_KEY'] = '${{ secrets.oci_cli_key_content }}'

        def mixed_case_notification_action(candidate):
            step(candidate, 'Send detailed Slack notification on failure')['env']['SIGNING_KEY'] = (
                '${{ secrets.Oci_Cli_Key_Content }}')

        def lowercase_script_interpolation(candidate):
            step(candidate, 'Verify OCI CLI version')['run'] += ' "${{ secrets.oci_cli_user }}"'

        def lowercase_reference_in_a_holder(candidate):
            step(candidate, 'Reconcile demo components')['env']['OCI_CLI_USER'] = '${{ secrets.oci_cli_user }}'

        # A name built at run time does not spell OCI_CLI_ anywhere.
        def name_built_with_format(candidate):
            candidate['jobs']['deploy']['env']['K'] = "${{ secrets[format('{0}_CLI_KEY_CONTENT', 'OCI')] }}"

        def name_built_with_format_in_a_script(candidate):
            step(candidate, 'Verify OCI CLI version')['run'] += (
                " \"${{ secrets[format('{0}_CLI_{1}', 'OCI', 'KEY_CONTENT')] }}\"")

        def lowercase_index_in_the_notification(candidate):
            step(candidate, 'Send detailed Slack notification on failure')['env']['K'] = (
                "${{ secrets['oci_cli_key_content'] }}")

        def cluster_secret_given_to_another_action(candidate):
            step(candidate, 'Install Helm')['with']['cluster'] = '${{ secrets.OKE_CLUSTER_OCID }}'

        def webhook_secret_given_to_a_script(candidate):
            step(candidate, 'Reconcile demo components')['env']['SLACK_WEBHOOK'] = '${{ secrets.SLACK_WEBHOOK_URL }}'

        for change in (job_env, job_env_under_another_name, workflow_env, job_container, notification_action,
                       helm_action, checkout_input, unpinned_configuration, script_interpolation, renamed_variable,
                       partial_block, crossed_names, lowercase_job_env, mixed_case_notification_action,
                       lowercase_script_interpolation, lowercase_reference_in_a_holder, name_built_with_format,
                       name_built_with_format_in_a_script, lowercase_index_in_the_notification,
                       cluster_secret_given_to_another_action, webhook_secret_given_to_a_script):
            with self.subTest(change=change.__name__):
                candidate = copy.deepcopy(workflow)
                change(candidate)
                self.assertNotEqual([], oci_credential_violations(candidate))

    def test_whole_secrets_search_ignores_case(self):
        for text in ('${{ toJSON(secrets) }}', '${{ tojson(secrets) }}', '${{ TOJSON( secrets ) }}',
                     'secrets: inherit', 'Secrets : Inherit'):
            with self.subTest(text=text):
                self.assertIsNotNone(WHOLE_SECRETS.search(text))
        self.assertIsNone(WHOLE_SECRETS.search('${{ secrets.SLACK_WEBHOOK }}'))

    def test_oci_cli_is_installed_with_required_hashes_before_any_step_holds_the_api_key(self):
        for filename in WRITERS:
            with self.subTest(workflow=filename):
                source = (WORKFLOWS / filename).read_text()
                steps = [step for job in yaml.safe_load(source)['jobs'].values() for step in job['steps']]
                names = [step.get('name') for step in steps]
                install = steps[names.index('Install the hash-verified OCI CLI')]
                # The build of a source distribution runs here, so this step holds no secret.
                self.assertEqual({'name': 'Install the hash-verified OCI CLI', 'env': {'PIP_CONSTRAINT': PIP_CONSTRAINT},
                                  'run': HASHED_INSTALL + '\n'}, install)
                self.assertEqual(1, source.count('pip install'))
                configure = names.index('Configure kubectl with OKE')
                self.assertEqual(names.index('Install the hash-verified OCI CLI') + 1, configure)
                self.assertEqual(configure, min(index for index, step in enumerate(steps)
                                                if 'OCI_CLI_KEY_CONTENT' in (step.get('env') or {})))
                self.assertLess(names.index('Checkout demo configuration'), configure - 1)
                # The action runs "python -m pip install oci-cli" itself. Without an index
                # it succeeds only when the hash-verified install left nothing to fetch.
                self.assertEqual(dict(OCI_ENV, PIP_CONSTRAINT=PIP_CONSTRAINT, PIP_NO_INDEX='1'), steps[configure]['env'])
                self.assertRegex(steps[configure]['uses'], KUBECTL_CONFIGURATION)
        # Credential-free CI runs the same two commands on a hosted runner, then the same check.
        tests, = [job for job in yaml.safe_load((WORKFLOWS / 'ci.yml').read_text())['jobs'].values()
                  if job.get('name') == 'Demo tests']
        rehearsal, = [step for step in tests['steps'] if 'pip install oci-cli' in step.get('run', '')]
        self.assertEqual({'PIP_CONSTRAINT': PIP_CONSTRAINT}, rehearsal['env'])
        commands = [line for line in rehearsal['run'].splitlines() if not line.startswith('#')]
        self.assertEqual([HASHED_INSTALL, 'PIP_NO_INDEX=1 python -m pip install oci-cli',
                          'python scripts/demo/verify_oci_cli.py'], commands)

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

    def test_no_workflow_or_script_takes_or_restores_a_backup(self):
        # Generated traces are disposable (#76): the backup and restore tooling and the recovery gate are gone.
        for path in ('.github/workflows/ci-opensearch-recovery.yml', 'examples/otel-demo/opensearch-recovery.sh',
                     'examples/otel-demo/opensearch-recovery.test.sh', 'examples/otel-demo/OPENSEARCH_RECOVERY.md',
                     'examples/otel-demo/opensearch-volume-snapshot-class.yaml', 'scripts/demo/recovery_policy.py',
                     'scripts/demo/recovery_test.py'):
            self.assertFalse((ROOT / path).exists(), path)
        inputs = yaml.safe_load((WORKFLOWS / 'ci-deploy-demo.yml').read_text())[True]['workflow_dispatch']['inputs']
        self.assertEqual({'expected_config_sha', 'image_tag', 'jaeger_image_tag', 'hotrod_image_tag', 'demo_stack',
                          'otel_deploy_scope'}, set(inputs))
        # Tests are left out, and so is deployer_access.py: its denied list names the snapshot kinds
        # so that `check` can show neither identity holds them.
        sources = sorted(WORKFLOWS.glob('*.yml'))
        sources += [path for path in sorted((ROOT / 'examples').rglob('*.sh')) + sorted((ROOT / 'scripts').rglob('*'))
                    if path.suffix in ('.py', '.sh') and not path.name.endswith(('_test.py', '.test.sh'))
                    and path.name != 'deployer_access.py']
        self.assertGreater(len(sources), 30)
        for path in sources:
            text = path.read_text().lower()
            for retired in ('volumesnapshot', 'snapshot.storage.k8s.io', 'opensearch_recovery', 'recovery_policy',
                            'opensearch-recovery.sh', 'backup-test', 'recovery_started', 'jaeger-os-verify'):
                with self.subTest(path=str(path.relative_to(ROOT)), retired=retired):
                    self.assertNotIn(retired, text)

    def test_reset_is_manual_typed_and_checked_before_cluster_access(self):
        import opensearch_reset
        source = (WORKFLOWS / 'ci-opensearch-reset.yml').read_text()
        workflow = yaml.safe_load(source)
        # A dispatch is the only trigger: no schedule, push or workflow_run can start a reset.
        self.assertEqual(['workflow_dispatch'], list(workflow[True]))
        inputs = workflow[True]['workflow_dispatch']['inputs']
        self.assertEqual({'operation', 'acknowledgment'}, set(inputs))
        # The default changes nothing, and no input names a namespace, a Pod or an index.
        self.assertEqual(('choice', 'list', ['list', 'reset']),
                         (inputs['operation']['type'], inputs['operation']['default'], inputs['operation']['options']))
        self.assertEqual('string', inputs['acknowledgment']['type'])
        self.assertNotIn('default', inputs['acknowledgment'])
        self.assertIn(opensearch_reset.ACKNOWLEDGMENT, inputs['acknowledgment']['description'])
        self.assertEqual(['require-main', 'reset'], list(workflow['jobs']))
        job = workflow['jobs']['reset']
        self.assertEqual(('demo-apply', 'require-main', "github.ref == 'refs/heads/main'"),
                         (job['environment'], job['needs'], job['if']))
        self.assertLessEqual(job['timeout-minutes'], 60)
        # The inputs are data: they reach the script through the environment, quoted.
        self.assertEqual(('${{ inputs.operation }}', '${{ inputs.acknowledgment }}'),
                         (job['env']['RESET_OPERATION'], job['env']['RESET_ACKNOWLEDGMENT']))
        command = 'python3 scripts/demo/opensearch_reset.py "$RESET_OPERATION" --acknowledge "$RESET_ACKNOWLEDGMENT"'
        runs = [step.get('run', '') for step in job['steps']]
        self.assertFalse([run for run in runs if '${{' in run])
        names = [step['name'] for step in job['steps']]
        self.assertEqual(['Checkout demo configuration', 'Check the request before any cluster access',
                          'Install the hash-verified OCI CLI', 'Configure kubectl with OKE', 'Verify OCI CLI version',
                          "Verify the deployment identity's access", 'List or reset the Jaeger trace indices'], names)
        self.assertEqual(command + ' --validate-only', runs[1])
        self.assertEqual(command, runs[6])
        # The acknowledgment is checked before the API key reaches any step.
        self.assertNotIn('OCI_CLI_KEY_CONTENT', job['steps'][1].get('env') or {})
        self.assertNotIn('continue-on-error', source)
        # The validation step really stops a wrong phrase, on the runner's shell, with no kubectl on the path.
        for phrase, status in ((opensearch_reset.ACKNOWLEDGMENT, 0), ('yes', 2), ('', 2)):
            with self.subTest(phrase=phrase), tempfile.TemporaryDirectory() as empty:
                env = dict(os.environ, PATH=empty + os.pathsep + os.path.dirname(sys.executable) + os.pathsep + '/usr/bin:/bin',
                           RESET_OPERATION='reset', RESET_ACKNOWLEDGMENT=phrase, GITHUB_EVENT_NAME='workflow_dispatch',
                           GITHUB_REF='refs/heads/main')
                result = subprocess.run(['bash', '-e', '-c', runs[1]], cwd=ROOT, env=env, text=True, capture_output=True)
                self.assertEqual(status, result.returncode, result.stderr)

    def test_dispatch_cannot_select_a_clean_run(self):
        # The deployer role cannot create or delete namespaces (docs/deployer-access.md),
        # so a clean run would leave the demo down. Only an administrator runs it.
        source = (WORKFLOWS / 'ci-deploy-demo.yml').read_text()
        workflow = yaml.safe_load(source)
        inputs = workflow[True]['workflow_dispatch']['inputs']
        self.assertNotIn('deploy_mode', inputs)
        for name, spec in inputs.items():
            with self.subTest(input=name):
                self.assertNotIn('clean', spec.get('options') or [])
        env = workflow['jobs']['deploy']['env']
        self.assertEqual('upgrade', env['JAEGER_DEMO_DEPLOY_MODE'])
        self.assertNotIn('deploy_mode', source)
        for path in sorted(WORKFLOWS.glob('*.yml')):
            with self.subTest(workflow=path.name):
                self.assertNotIn('inputs.deploy_mode', path.read_text())

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

    def test_adoption_job_rehearses_the_trace_reset_on_its_own_cluster(self):
        job = yaml.safe_load((WORKFLOWS / 'ci.yml').read_text())['jobs']['adoption']
        runs = [step['run'] for step in job['steps'] if 'run' in step]
        rehearsal, = [run for run in runs if 'rehearsals/opensearch-reset/exercise.py' in run]
        self.assertIn('--context kind-jaeger-demo-reset-fixture', rehearsal)
        self.assertTrue((ROOT / 'scripts/demo/rehearsals/opensearch-reset/exercise.py').is_file())
        # It follows the other rehearsals, which need a cluster with only the bootstrap namespaces.
        self.assertEqual(len(runs) - 1, runs.index(rehearsal))

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


if __name__ == '__main__':
    unittest.main()
