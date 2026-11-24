# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""The pure parts of the deployer access rehearsal. The `deployer-access` CI job runs the rest on kind."""

import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import unittest

import yaml

import deployer_access

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "deployer_access_rehearsal", ROOT / "scripts/demo/rehearsals/deployer-access/exercise.py")
rehearsal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rehearsal)

SOURCE = {
    "apiVersion": "v1", "kind": "Config", "current-context": "other",
    "clusters": [{"name": "kind", "cluster": {"server": "https://127.0.0.1:6443"}}],
    "contexts": [{"name": rehearsal.CONTEXT, "context": {"cluster": "kind", "user": "admin"}},
                 {"name": "other", "context": {"cluster": "kind", "user": "admin"}}],
    "users": [{"name": "admin", "user": {"client-key-data": "a2V5"}}],
}
REFUSED = ('Error: INSTALLATION FAILED: unable to continue with install: could not get information about the '
           'resource ConfigMap "deployer-access-probe" in namespace "kube-system": configmaps '
           '"deployer-access-probe" is forbidden: User "ocid1.user.oc1..aaaafixturescheduled" cannot get '
           'resource "configmaps" in API group "" in the namespace "kube-system"')


class KubeconfigTests(unittest.TestCase):
    def write(self, **kwargs):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = rehearsal.kubeconfig(SOURCE, rehearsal.CONTEXT, Path(temp.name) / "config.yaml", **kwargs)
        return path, yaml.safe_load(path.read_text())

    def test_impersonating_copy_selects_the_context_and_is_private(self):
        path, config = self.write(user=rehearsal.SCHEDULED)
        self.assertEqual(rehearsal.CONTEXT, config["current-context"])
        self.assertEqual([{"name": "admin", "user": {"client-key-data": "a2V5", "as": rehearsal.SCHEDULED}}],
                         config["users"])
        self.assertEqual(0o600, stat.S_IMODE(os.stat(path).st_mode))
        # The caller's kubeconfig is not changed.
        self.assertNotIn("as", SOURCE["users"][0]["user"])
        self.assertEqual("other", SOURCE["current-context"])

    def test_administrator_copy_does_not_impersonate(self):
        _, config = self.write()
        self.assertEqual(rehearsal.CONTEXT, config["current-context"])
        self.assertEqual(SOURCE["users"], config["users"])

    def test_unknown_context_and_existing_file_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.yaml"
            with self.assertRaisesRegex(ValueError, "no context kind-elsewhere"):
                rehearsal.kubeconfig(SOURCE, "kind-elsewhere", path)
            self.assertFalse(path.exists())
            path.write_text("kept\n")
            with self.assertRaises(FileExistsError):
                rehearsal.kubeconfig(SOURCE, rehearsal.CONTEXT, path, user=rehearsal.SCHEDULED)
            self.assertEqual("kept\n", path.read_text())


class RefusalTests(unittest.TestCase):
    def test_a_forbidden_answer_yields_its_verb_and_resource(self):
        self.assertEqual([("get", "configmaps")], rehearsal.refusals(REFUSED, rehearsal.SCHEDULED, "kube-system"))

    def test_a_quoted_forbidden_answer_still_matches(self):
        quoted = REFUSED.replace('"', '\\"')
        self.assertEqual([("get", "configmaps")], rehearsal.refusals(quoted, rehearsal.SCHEDULED, "kube-system"))

    def test_another_user_or_namespace_is_not_this_refusal(self):
        self.assertEqual([], rehearsal.refusals(REFUSED, rehearsal.MANUAL, "kube-system"))
        self.assertEqual([], rehearsal.refusals(REFUSED.replace('"kube-system"', '"default"'),
                                                rehearsal.SCHEDULED, "kube-system"))

    def test_failures_that_are_not_an_authorization_answer_do_not_match(self):
        for text in ("Error: INSTALLATION FAILED: context deadline exceeded",
                     "Error: Kubernetes cluster unreachable: Get \"https://127.0.0.1:6443/version\": i/o timeout",
                     "forbidden", "Error: secrets is forbidden",
                     'secrets is forbidden: User "ocid1.user.oc1..aaaafixturescheduled" cannot list resource '
                     '"secrets" in API group "" at the cluster scope'):
            with self.subTest(text=text):
                self.assertEqual([], rehearsal.refusals(text, rehearsal.SCHEDULED, "kube-system"))

    def test_a_list_refusal_names_the_collection(self):
        text = ('Error: list: failed to list: secrets is forbidden: User "ocid1.user.oc1..aaaafixturescheduled" '
                'cannot list resource "secrets" in API group "" in the namespace "kube-system"')
        self.assertEqual([("list", "secrets")], rehearsal.refusals(text, rehearsal.SCHEDULED, "kube-system"))


class RuleRemovalTests(unittest.TestCase):
    def setUp(self):
        self.documents = list(yaml.safe_load_all(deployer_access.ROLES.read_text()))

    def test_removes_only_the_named_kube_system_services_rule(self):
        reduced, removed = rehearsal.without_rule(self.documents, "kube-system", ["services"], named=True)
        self.assertEqual(["services"], removed["resources"])
        self.assertTrue(removed["resourceNames"])
        self.assertTrue(all(name.startswith("prometheus-kube-prometheus-") for name in removed["resourceNames"]))
        before = {(d["metadata"]["name"], d["metadata"].get("namespace", "")): d["rules"] for d in self.documents}
        after = {(d["metadata"]["name"], d["metadata"].get("namespace", "")): d["rules"] for d in reduced}
        key = (deployer_access.DEPLOYER, "kube-system")
        self.assertEqual([rule for rule in before[key] if rule != removed], after[key])
        self.assertEqual(len(before[key]) - 1, len(after[key]))
        self.assertEqual({k: v for k, v in before.items() if k != key}, {k: v for k, v in after.items() if k != key})
        # The committed documents are not changed.
        self.assertIn(removed, before[key])

    def test_the_removed_rule_leaves_required_requests_unexplained(self):
        reduced, removed = rehearsal.without_rule(self.documents, "kube-system", ["services"], named=True)
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "reduced.yaml"
            path.write_text(yaml.safe_dump_all(reduced))
            smaller = deployer_access.load_roles(path)
        committed = deployer_access.load_roles()
        lost = [r for r in deployer_access.required("scheduled")
                if deployer_access.allowed("scheduled", r, committed) and not deployer_access.allowed("scheduled", r, smaller)]
        self.assertEqual(len(removed["resourceNames"]) * len(removed["verbs"]), len(lost))

    def test_no_or_ambiguous_match_is_refused(self):
        for namespace, resources, named in (("kube-system", ["secrets"], True), ("cert-manager", ["services"], True),
                                            ("default", ["pods"], True)):
            with self.subTest(namespace=namespace, resources=resources):
                with self.assertRaisesRegex(ValueError, "Expected one deployer rule"):
                    rehearsal.without_rule(self.documents, namespace, resources, named)


class CheckOutputTests(unittest.TestCase):
    def test_parses_both_kinds_of_difference(self):
        missing = deployer_access.Request("kube-system", "", "services", "get", "prometheus-kube-prometheus-coredns")
        extra = deployer_access.Request("kube-system", "", "secrets", "get", "")
        output = "\n".join([
            "missing: " + json_line(missing), "must not be allowed: " + json_line(extra),
            "scheduled: 2 difference(s) from ops/rbac/deployer.yaml"])
        self.assertEqual([(missing, True), (extra, False)], rehearsal.differences(output))
        self.assertEqual([], rehearsal.differences("scheduled: 0 difference(s) from ops/rbac/deployer.yaml"))


class ProductionPathTests(unittest.TestCase):
    def test_admin_install_uses_the_flags_and_values_of_the_monitoring_upgrade(self):
        script = (ROOT / "examples/oci/deploy-all.sh").read_text()
        words = rehearsal.monitoring_command(script)
        self.assertEqual(["--timeout", "20m0s", "prometheus", "$PROMETHEUS_STACK_CHART",
                          "--set", "crds.upgradeJob.enabled=true", "--set", "crds.upgradeJob.forceConflicts=true",
                          "-f", "monitoring-values.yaml"], words)

    def test_the_scheduled_upgrade_uses_helms_default_wait(self):
        script = (ROOT / "examples/oci/deploy-all.sh").read_text()
        # The administrator installs as a first manual run would; a scheduled run only upgrades.
        self.assertEqual(["upgrade --install --wait", "upgrade --wait", "install --wait"],
                         re.findall(r"HELM_PROM_CMD=\(([^)]*)\)", script))
        self.assertNotIn("--wait=", script)

    def test_monitoring_command_must_be_unique_and_complete(self):
        with self.assertRaisesRegex(ValueError, "one monitoring helm command"):
            rehearsal.monitoring_command("echo nothing\n")
        with self.assertRaisesRegex(ValueError, "no longer names"):
            rehearsal.monitoring_command('helm "${HELM_PROM_CMD[@]}" --timeout 20m0s other "$CHART"\n')

    def test_scheduled_environment_matches_a_scheduled_monitoring_run(self):
        base = {"PATH": "/bin", "HOME": "/home/runner", "KUBECONFIG": "/admin", "HELM_KUBEASUSER": "admin",
                "HELM_NAMESPACE": "kube-system", "EXPECTED_CONFIG_SHA": "1" * 40,
                "PROMETHEUS_STACK_CHART_VERSION": "1.0.0", "JAEGER_DEMO_IMAGE_TAG": "x",
                "GITHUB_STEP_SUMMARY": "/summary", "RUN_PUBLIC_SMOKE_TESTS": "true"}
        env = rehearsal.scheduled_env(base, Path("/fixture/scheduled.yaml"))
        self.assertEqual({
            "PATH": "/bin", "HOME": "/home/runner", "KUBECONFIG": "/fixture/scheduled.yaml",
            "GITHUB_EVENT_NAME": "schedule", "GITHUB_REF": "refs/heads/main", "JAEGER_DEMO_STACK": "oci",
            "JAEGER_DEMO_DEPLOY_MODE": "upgrade", "JAEGER_DEMO_DEPLOY_SCOPE": "monitoring",
            "JAEGER_OTEL_DEMO_DEPLOY_SCOPE": "monitoring", "RUN_PUBLIC_SMOKE_TESTS": "false"}, env)
        # deploy.py gives deploy-all.sh the same scope variables for the oci-monitoring component.
        source = (ROOT / "scripts/demo/deploy.py").read_text()
        self.assertIn("JAEGER_DEMO_DEPLOY_SCOPE=scope", source)
        self.assertIn('"oci-monitoring": ("oci", "monitoring"', source)


def json_line(request):
    return json.dumps(request._asdict(), sort_keys=True)


if __name__ == "__main__":
    unittest.main()
