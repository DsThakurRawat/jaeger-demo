# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

from itertools import takewhile
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import deploy


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.desired = {name: deploy.fingerprint(name) for name in deploy.COMPONENTS}
        self.env = {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main"}

    def test_command_failures_propagate(self):
        result = deploy.run([sys.executable, "-c", "print('ready')"], capture_output=True)
        self.assertEqual("ready\n", result.stdout)
        with self.assertRaises(subprocess.CalledProcessError) as error:
            deploy.run([sys.executable, "-c", "raise SystemExit(3)"])
        self.assertEqual(3, error.exception.returncode)

    def test_unchanged_components_are_skipped_and_failed_revisions_retry(self):
        self.assertEqual([], deploy.selected_components("push", "oci", "jaeger", "upgrade", self.desired, self.desired))
        previous = dict(self.desired, opensearch="old")
        self.assertEqual(["opensearch"], deploy.selected_components("push", "oci", "jaeger", "upgrade", previous, self.desired))

    def test_daily_refreshes_both_jaeger_stacks(self):
        self.assertEqual(["otel-jaeger", "oci-jaeger"], deploy.selected_components(
            "schedule", "oci", "jaeger", "upgrade", self.desired, self.desired))

    def test_schedule_reconciles_all_five_components_when_inputs_change(self):
        self.assertEqual(list(deploy.COMPONENTS), deploy.selected_components(
            "schedule", "oci", "jaeger", "upgrade", {}, self.desired))
        for name in ("opensearch", "otel-demo", "oci-monitoring"):
            previous = dict(self.desired, **{name: "old"})
            self.assertEqual({name, "oci-jaeger", "otel-jaeger"}, set(deploy.selected_components(
                "schedule", "oci", "jaeger", "upgrade", previous, self.desired)))

    @patch("deploy.handover.read", return_value="default")
    @patch("deploy.run")
    def test_schedule_requires_existing_revision_state(self, run, read):
        run.return_value.stdout = ""
        with self.assertRaisesRegex(ValueError, "existing deployment revision"):
            deploy.reconcile(dict(self.env, GITHUB_EVENT_NAME="schedule"))
        self.assertEqual(1, run.call_count)

    @patch("deploy.handover.read")
    @patch("deploy.run")
    def test_schedule_rejects_invalid_context_clean_and_recovery_policy_before_mutation(self, run, read):
        for overrides, namespace in (({"HELM_DRIVER": "configmap"}, ""), ({"HELM_NAMESPACE": "test"}, ""),
                                     ({"HELM_KUBECONTEXT": "another-cluster"}, ""),
                                     ({}, "other"), ({"JAEGER_DEMO_DEPLOY_MODE": "clean"}, ""),
                                     ({"JAEGER_OTEL_DEMO_OPENSEARCH_RECOVERY": "waive"}, ""),
                                     ({"JAEGER_OTEL_DEMO_OPENSEARCH_RECOVERY": "invalid"}, "")):
            with self.subTest(overrides=overrides, namespace=namespace):
                read.return_value = namespace
                with self.assertRaises(ValueError):
                    deploy.reconcile(dict(self.env, GITHUB_EVENT_NAME="schedule", **overrides))
                run.assert_not_called()

    @patch("deploy.run")
    def test_schedule_checks_all_existing_releases_before_mutating_component(self, run):
        run.return_value.stdout = json.dumps({"info": {"status": "deployed"}})
        for component in deploy.COMPONENTS:
            deploy.check_scheduled_releases(component)
        self.assertEqual([("opensearch", "opensearch"), ("opensearch", "opensearch-dashboards"),
                          ("jaeger", "jaeger"), ("otel-demo", "otel-demo"),
                          ("default", "jaeger"), ("default", "prometheus")],
                         [(args[4], args[2]) for call in run.call_args_list for args in [call.args[0]]])

    @patch("deploy.run")
    def test_pending_or_missing_storage_stops_before_scheduled_upgrade(self, run):
        for error in (subprocess.CalledProcessError(1, "helm status"), None):
            with self.subTest(error=error):
                run.reset_mock()
                healthy = subprocess.CompletedProcess([], 0, stdout='{"info":{"status":"deployed"}}')
                pending = subprocess.CompletedProcess([], 0, stdout='{"info":{"status":"pending-upgrade"}}')
                run.side_effect = [healthy, error or pending]
                with self.assertRaises((deploy.ScheduledReleaseError, subprocess.CalledProcessError)):
                    deploy.deploy_component("opensearch", dict(self.env, GITHUB_EVENT_NAME="schedule"))
                self.assertEqual([["helm", "status"]] * 2, [c.args[0][:2] for c in run.call_args_list])

    @patch("deploy.run")
    def test_failed_monitoring_can_retry_in_place_but_failed_jaeger_cannot(self, run):
        run.return_value.stdout = '{"info":{"status":"failed"}}'
        deploy.check_scheduled_releases("oci-monitoring")
        for name in ("oci-jaeger", "otel-jaeger"):
            with self.subTest(component=name), self.assertRaises(deploy.ScheduledReleaseError):
                deploy.check_scheduled_releases(name)

    @patch("deploy.handover.read", return_value="default")
    @patch("deploy.resolve_snapshots")
    @patch("deploy.deploy_component")
    @patch("deploy.run")
    def test_schedule_patches_only_its_key_and_does_not_recreate_deleted_state(self, run, component, resolve, read):
        for deleted in (False, True):
            state = self.desired.copy()
            def command(args, **kwargs):
                if args[3] == "get":
                    return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"data": state}))
                self.assertEqual(["kubectl", "-n", "default", "patch", "configmap", deploy.STATE,
                                  "--type=merge", "-p"], args[:-1])
                patch = json.loads(args[-1])
                self.assertEqual({"data": {component.call_args.args[0]: self.desired[component.call_args.args[0]]}}, patch)
                if deleted:
                    raise subprocess.CalledProcessError(1, args)
                state["otel-demo"] = "independent-operator-update"
                state.update(patch["data"])
            run.side_effect = command
            with self.subTest(deleted=deleted):
                self.assertEqual(int(deleted), deploy.reconcile(dict(self.env, GITHUB_EVENT_NAME="schedule")))
                if not deleted:
                    self.assertEqual("independent-operator-update", state["otel-demo"])

    def test_manual_routing_and_clean(self):
        for name, (stack, scope, _) in deploy.COMPONENTS.items():
            self.assertEqual([name], deploy.selected_components("workflow_dispatch", stack, scope, "upgrade", {}, self.desired))
        self.assertEqual(list(deploy.COMPONENTS), deploy.selected_components("workflow_dispatch", "all", "all", "clean", {}, self.desired))

    def test_component_changes_do_not_refresh_unrelated_infrastructure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            shutil.copytree(deploy.ROOT / "examples/otel-demo", root / "examples/otel-demo")
            shutil.copytree(deploy.ROOT / "scripts/demo", root / "scripts/demo")
            script = root / "examples/otel-demo/deploy-all.sh"
            script.write_text(re.sub(r'OPENSEARCH_CHART_VERSION="[^\"]+"', 'OPENSEARCH_CHART_VERSION="99.99.99"', script.read_text()))
            self.assertNotEqual(self.desired["opensearch"], deploy.fingerprint("opensearch", root))
            self.assertEqual(self.desired["otel-jaeger"], deploy.fingerprint("otel-jaeger", root))
            self.assertEqual(self.desired["otel-demo"], deploy.fingerprint("otel-demo", root))
            values = root / "examples/otel-demo/otel-demo-values.yaml"
            values.write_text(values.read_text() + "\n# revised application\n")
            self.assertNotEqual(self.desired["otel-demo"], deploy.fingerprint("otel-demo", root))

    @patch("deploy.run")
    def test_schedule_needs_a_unique_persistent_claim_mounted_by_the_database(self, run):
        for volumes in ([], [{"name": "sidecar", "persistentVolumeClaim": {"claimName": "wrong"}}],
                        [{"name": "data", "emptyDir": {}}]):
            with self.subTest(volumes=volumes):
                run.return_value.stdout = json.dumps({"spec": {"containers": [{"name": "opensearch",
                    "volumeMounts": [{"name": "data", "mountPath": "/usr/share/opensearch/data"}]}], "volumes": volumes}})
                with self.assertRaisesRegex(deploy.ScheduledReleaseError, "existing OpenSearch data PVC"):
                    deploy.opensearch_data_binding()

    @patch("deploy.run")
    def test_scheduled_opensearch_preserves_bound_data_and_verifies_trace_without_recovery(self, run):
        pod = {"spec": {"containers": [{"name": "opensearch", "volumeMounts": [
            {"name": "data", "mountPath": "/usr/share/opensearch/data"}]}],
            "volumes": [{"name": "data", "persistentVolumeClaim": {"claimName": "source"}}]}}
        claim = {"metadata": {"uid": "existing"}, "spec": {"volumeName": "live"}, "status": {"phase": "Bound"}}
        changed = dict(claim, metadata={"uid": "replacement"})
        cases = {"success": [claim, claim], "rebound": [claim, changed], "different PV": [
            claim, dict(claim, spec={"volumeName": "other"})], "pending": [dict(claim, status={"phase": "Pending"})],
            "terminating": [dict(claim, metadata={"uid": "existing", "deletionTimestamp": "now"})]}
        for case, claims in cases.items():
            with self.subTest(case=case), tempfile.NamedTemporaryFile() as output:
                calls = []
                observations = iter(claims)
                def command(args, **kwargs):
                    calls.append(args)
                    if args[:2] == ["helm", "status"]:
                        data = {"info": {"status": "deployed"}}
                    elif args[:5] == ["kubectl", "-n", "opensearch", "get", "pod"]:
                        data = pod
                    elif args == ["kubectl", "-n", "opensearch", "get", "pvc", "source", "-o", "json"]:
                        data = next(observations)
                    else:
                        self.assertIn(Path(args[1]).name, ("deploy-all.sh", "verify_trace.py"))
                        data = {}
                    return subprocess.CompletedProcess(args, 0, stdout=json.dumps(data))
                run.side_effect = command
                env = dict(self.env, GITHUB_EVENT_NAME="schedule", GITHUB_OUTPUT=output.name)
                if case == "success":
                    deploy.deploy_component("opensearch", env)
                    self.assertEqual("verify_trace.py", Path(calls[-1][1]).name)
                else:
                    with self.assertRaises(deploy.ScheduledReleaseError):
                        deploy.deploy_component("opensearch", env)
                    self.assertFalse(any(Path(args[1]).name == "verify_trace.py" for args in calls))
                self.assertEqual("", Path(output.name).read_text())
                self.assertEqual(case in ("success", "rebound", "different PV"),
                                 any(Path(args[1]).name == "deploy-all.sh" for args in calls))

    @patch("deploy.run")
    def test_manual_default_recovery_precedes_upgrade_and_fresh_trace(self, run):
        deploy.deploy_component("opensearch", dict(self.env, GITHUB_EVENT_NAME="workflow_dispatch"))
        self.assertEqual("backup-test", run.call_args_list[0].args[0][-1])
        self.assertTrue(run.call_args_list[1].args[0][1].endswith("deploy-all.sh"))
        self.assertTrue(run.call_args_list[2].args[0][1].endswith("verify_trace.py"))
        self.assertEqual("opensearch", run.call_args_list[1].kwargs["env"]["JAEGER_DEMO_DEPLOY_SCOPE"])

    @patch("deploy.run", side_effect=subprocess.CalledProcessError(1, "recovery"))
    def test_manual_recovery_failure_prevents_helm_upgrade(self, run):
        with self.assertRaises(subprocess.CalledProcessError):
            deploy.deploy_component("opensearch", dict(self.env, GITHUB_EVENT_NAME="workflow_dispatch"))
        self.assertEqual(1, run.call_count)

    @patch("deploy.run")
    def test_explicit_manual_waiver_skips_only_backup(self, run):
        deploy.deploy_component("opensearch", dict(self.env, GITHUB_EVENT_NAME="workflow_dispatch",
                                                  JAEGER_OTEL_DEMO_OPENSEARCH_RECOVERY="waive"))
        self.assertEqual(2, run.call_count)

    @patch("deploy.run")
    def test_snapshot_exports_are_loaded_and_push_can_use_published_fallback(self, run):
        def export(*args, **kwargs):
            Path(kwargs["env"]["GITHUB_ENV"]).write_text("JAEGER_DEMO_JAEGER_IMAGE_TAG=abc\nIGNORED=x\n")
        run.side_effect = export
        deploy.resolve_snapshots(self.env)
        self.assertEqual("abc", self.env["JAEGER_DEMO_JAEGER_IMAGE_TAG"])
        self.assertNotIn("IGNORED", self.env)
        self.assertEqual("schedule", run.call_args.kwargs["env"]["GITHUB_EVENT_NAME"])

    @patch("deploy.resolve_snapshots")
    @patch("deploy.deploy_component")
    @patch("deploy.run")
    def test_failed_component_is_not_recorded_and_independent_components_continue(self, run, component, resolve):
        run.return_value.stdout = ""
        component.side_effect = [subprocess.CalledProcessError(1, "recovery"), None, None]
        self.assertEqual(1, deploy.reconcile(self.env))
        self.assertEqual(["opensearch", "oci-jaeger", "oci-monitoring"], [call.args[0] for call in component.call_args_list])
        states = [json.loads(call.kwargs["input"])["data"] for call in run.call_args_list if "input" in call.kwargs]
        self.assertNotIn("opensearch", states[-1])
        self.assertEqual({"oci-jaeger", "oci-monitoring"}, set(states[-1]))

    @patch("deploy.resolve_snapshots")
    @patch("deploy.deploy_component")
    @patch("deploy.run")
    def test_only_successful_component_revisions_are_written(self, run, component, resolve):
        previous = dict(self.desired, **{"otel-demo": "old"})
        run.return_value.stdout = json.dumps({"data": previous})
        with tempfile.NamedTemporaryFile() as summary:
            self.env["GITHUB_STEP_SUMMARY"] = summary.name
            self.assertEqual(0, deploy.reconcile(self.env))
            self.assertIn("otel-demo: deployed", Path(summary.name).read_text())
        component.assert_called_once_with("otel-demo", self.env)
        resolve.assert_not_called()
        self.assertEqual(self.desired, json.loads(run.call_args.kwargs["input"])["data"])

    def run_workflow_step(self, step, kubectl_status, **env):
        lines = step.split("\n      run: |\n", 1)[1].splitlines()
        script = "\n".join(line[8:] for line in takewhile(lambda line: line.startswith(" " * 8) or not line, lines))
        self.assertNotIn("${{", script)
        with tempfile.TemporaryDirectory() as temp:
            kubectl = Path(temp) / "kubectl"
            kubectl.write_text('#!/bin/sh\necho "$*" >> "$TEST_KUBECTL_LOG"\nexit "$TEST_KUBECTL_STATUS"\n')
            kubectl.chmod(0o755)
            log = Path(temp) / "kubectl.log"
            env = dict(os.environ, PATH=temp + os.pathsep + os.environ["PATH"], TEST_KUBECTL_LOG=str(log),
                       TEST_KUBECTL_STATUS=str(kubectl_status), **env)
            result = subprocess.run(["bash", "-e", "-c", script], env=env, text=True, capture_output=True)
            return result, log.read_text().splitlines()

    def test_deploy_workflow_guards_the_restore_test(self):
        workflows = deploy.ROOT / ".github/workflows"
        workflow = (workflows / "ci-deploy-demo.yml").read_text()
        job, *steps = workflow.split("\n    - name: ")
        names = [step.split("\n", 1)[0] for step in steps]
        self.assertEqual(["Reconcile demo components", "Collect OpenSearch recovery diagnostics",
                          "Remove disposable restore pod", "Send detailed Slack notification on failure"], names[-4:])
        self.assertNotIn("continue-on-error", workflow)
        limit = re.search(r"^    timeout-minutes: (\d+)$", job, re.MULTILINE)
        self.assertIsNotNone(limit, "the deploy job has no timeout-minutes")
        recovery_limit = re.search(r"^    timeout-minutes: (\d+)$", (workflows / "ci-opensearch-recovery.yml").read_text(), re.MULTILINE)
        self.assertLess(int(recovery_limit[1]), int(limit[1]))
        self.assertLess(int(limit[1]), 360)

        diagnostics, cleanup, slack = steps[-3:]
        self.assertIn("\n      if: failure() && github.event_name == 'workflow_dispatch' && steps.configure-oke.outcome == 'success' && steps.reconcile.outputs.recovery_started == 'true'\n", diagnostics)
        self.assertIn("\n      if: always() && github.event_name == 'workflow_dispatch' && steps.configure-oke.outcome == 'success' && "
                      "steps.reconcile.outputs.recovery_started == 'true' && "
                      "(steps.reconcile.outcome == 'success' || steps.reconcile.outcome == 'failure' || "
                      "steps.reconcile.outcome == 'cancelled')\n", cleanup)
        self.assertIn("\n      if: failure()\n", slack)
        self.assertIn("\n        OPENSEARCH_RECOVERY_ID: ${{ github.run_id }}-${{ github.run_attempt }}\n", cleanup)
        script = deploy.ROOT / "examples/otel-demo/opensearch-recovery.sh"
        self.assertIn('local restore_pod="jaeger-os-verify-$RECOVERY_ID"', script.read_text())
        run = dict(GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2", OPENSEARCH_RECOVERY_ID="")
        derived = subprocess.run(["bash", "-c", 'source "$1" && echo "$RECOVERY_ID"', "bash", str(script)],
                                 env=dict(os.environ, **run), text=True, capture_output=True, check=True)
        self.assertEqual("123-2", derived.stdout.strip())
        selector = " -l jaegertracing.io/recovery-source=opensearch -o custom-columns="
        for status in (0, 1):
            with self.subTest(kubectl_status=status):
                result, calls = self.run_workflow_step(diagnostics, status)
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(["volumesnapshots", "pvc", "pods"], [call.split()[3] for call in calls])
                self.assertTrue(all(call.startswith("-n opensearch get ") and selector in call for call in calls))
                result, calls = self.run_workflow_step(cleanup, status, OPENSEARCH_RECOVERY_ID="123-2")
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(["-n opensearch delete pod jaeger-os-verify-123-2 --ignore-not-found=true --wait=false"], calls)
                self.assertEqual(bool(status), "::warning::" in result.stdout)

    @patch("deploy.resolve_snapshots")
    @patch("deploy.run")
    def test_clean_is_dispatched_once_per_stack(self, run, resolve):
        env = dict(self.env, GITHUB_EVENT_NAME="workflow_dispatch", JAEGER_DEMO_STACK="all", JAEGER_DEMO_DEPLOY_MODE="clean")
        self.assertEqual(0, deploy.reconcile(env))
        self.assertEqual(2, run.call_count)
        self.assertTrue(all(call.args[0][-1] == "clean" for call in run.call_args_list))

    def test_rejects_invalid_or_automatic_unsafe_routes(self):
        cases = [{"GITHUB_REF": "refs/heads/other"}, {"JAEGER_DEMO_STACK": "invalid"},
                 {"JAEGER_DEMO_DEPLOY_MODE": "invalid"}, {"JAEGER_DEMO_DEPLOY_MODE": "clean"},
                 {"GITHUB_EVENT_NAME": "pull_request"}, {"JAEGER_OTEL_DEMO_OPENSEARCH_RECOVERY": "waive"}]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                deploy.reconcile(dict(self.env, **case))

    @patch("deploy.run")
    def test_manual_invalid_scope_fails(self, run):
        run.return_value.stdout = json.dumps({"data": self.desired})
        with self.assertRaises(ValueError):
            deploy.reconcile(dict(self.env, GITHUB_EVENT_NAME="workflow_dispatch", JAEGER_OTEL_DEMO_DEPLOY_SCOPE="invalid"))

    def handover_env(self, **overrides):
        return dict(self.env, **dict({
            "GITHUB_EVENT_NAME": "workflow_dispatch", "EXPECTED_CONFIG_SHA": "a" * 40, "GITHUB_SHA": "a" * 40,
            "JAEGER_DEMO_STACK": "oci", "JAEGER_DEMO_DEPLOY_MODE": "upgrade", "JAEGER_OTEL_DEMO_DEPLOY_SCOPE": "jaeger",
            "JAEGER_DEMO_JAEGER_IMAGE_TAG": "b" * 40, "JAEGER_DEMO_HOTROD_IMAGE_TAG": "c" * 40,
        }, **overrides))

    @patch("deploy.run")
    def test_unsafe_handover_routes_fail_before_any_command(self, run):
        for overrides in ({"GITHUB_EVENT_NAME": "schedule"}, {"JAEGER_DEMO_STACK": "all"},
                          {"JAEGER_DEMO_DEPLOY_MODE": "clean"}, {"JAEGER_OTEL_DEMO_DEPLOY_SCOPE": "monitoring"},
                          {"JAEGER_DEMO_JAEGER_IMAGE_TAG": "latest"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                deploy.reconcile(self.handover_env(**overrides))
        run.assert_not_called()

    @patch("deploy.handover.preflight")
    @patch("deploy.resolve_snapshots")
    @patch("deploy.deploy_component")
    @patch("deploy.run")
    def test_handover_preflight_precedes_deployment_and_state_write(self, run, component, resolve, preflight):
        run.return_value.stdout = json.dumps({"data": self.desired})
        calls = []
        preflight.side_effect = lambda *args: calls.append("preflight")
        component.side_effect = lambda *args: calls.append("deploy")
        def command(*args, **kwargs):
            if "input" in kwargs:
                calls.append("state")
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"data": self.desired}))
        run.side_effect = command
        env = self.handover_env()
        self.assertEqual(0, deploy.reconcile(env))
        self.assertEqual(["preflight", "deploy", "state"], calls)
        component.assert_called_once_with("oci-jaeger", env)
        preflight.assert_called_once_with(env, "oci", "upgrade", "jaeger")

    @patch("deploy.handover.preflight", side_effect=subprocess.CalledProcessError(1, "kubectl", stderr="NotFound"))
    @patch("deploy.deploy_component")
    @patch("deploy.run")
    def test_missing_handover_object_prevents_component_and_state_mutations(self, run, component, preflight):
        run.return_value.stdout = ""
        with self.assertRaises(subprocess.CalledProcessError):
            deploy.reconcile(self.handover_env())
        component.assert_not_called()
        self.assertEqual(1, run.call_count)
        self.assertNotIn("input", run.call_args.kwargs)

    @patch("deploy.selected_components", return_value=["oci-jaeger", "oci-monitoring"])
    @patch("deploy.handover.preflight")
    @patch("deploy.deploy_component")
    @patch("deploy.run")
    def test_unexpected_handover_component_selection_fails_closed(self, run, component, preflight, selected):
        run.return_value.stdout = ""
        with self.assertRaisesRegex(ValueError, "selected components"):
            deploy.reconcile(self.handover_env())
        component.assert_not_called()
        preflight.assert_not_called()
        self.assertEqual(1, run.call_count)

    @patch("deploy.handover.preflight")
    @patch("deploy.resolve_snapshots")
    @patch("deploy.deploy_component")
    @patch("deploy.run")
    def test_changed_handover_image_resolution_prevents_all_writes(self, run, component, resolve, preflight):
        run.return_value.stdout = ""
        resolve.side_effect = lambda env: env.update(JAEGER_DEMO_JAEGER_IMAGE_TAG="d" * 40)
        with self.assertRaisesRegex(ValueError, "image resolution changed"):
            deploy.reconcile(self.handover_env())
        component.assert_not_called()
        self.assertEqual(1, run.call_count)

    @patch("deploy.run")
    def test_cleanup_signal_is_set_only_when_recovery_backup_starts(self, run):
        for component, mode, policy, recovery in (("opensearch", "upgrade", "required", True),
                                                  ("opensearch", "upgrade", "waive", False),
                                                  ("opensearch", "clean", "required", False),
                                                  ("oci-jaeger", "upgrade", "required", False),
                                                  ("otel-jaeger", "upgrade", "required", False)):
            with self.subTest(component=component, mode=mode, policy=policy), tempfile.NamedTemporaryFile() as output:
                run.reset_mock()
                env = dict(self.env, GITHUB_EVENT_NAME="workflow_dispatch", JAEGER_DEMO_DEPLOY_MODE=mode,
                           JAEGER_OTEL_DEMO_OPENSEARCH_RECOVERY=policy, GITHUB_OUTPUT=output.name)
                def command(args, **kwargs):
                    if args[-1] == "backup-test":
                        self.assertEqual("recovery_started=true\n", Path(output.name).read_text())
                        raise subprocess.CalledProcessError(1, args)
                run.side_effect = command
                if recovery:
                    with self.assertRaises(subprocess.CalledProcessError):
                        deploy.deploy_component(component, env)
                else:
                    deploy.deploy_component(component, env)
                self.assertEqual("recovery_started=true\n" if recovery else "", Path(output.name).read_text())


if __name__ == "__main__":
    unittest.main()
