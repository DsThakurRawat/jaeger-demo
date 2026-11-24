# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""The trace reset touches its named resources only and refuses everything else."""

import contextlib
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

import yaml

import deployer_access
import opensearch_reset as reset


ROOT = Path(__file__).resolve().parents[2]
TRACES = ["jaeger-main-jaeger-dependencies-2026-11-23", "jaeger-main-jaeger-sampling-2026-11-23",
          "jaeger-main-jaeger-service-2026-11-23", "jaeger-main-jaeger-service-2026-11-24",
          "jaeger-main-jaeger-span-2026-11-23", "jaeger-main-jaeger-span-2026-11-24"]
# Everything here looks close to a trace index, or matters to another component, and must survive.
OTHERS = [".kibana_1", ".opensearch-observability", "jaeger-archive-jaeger-span-2026-11-23",
          "jaeger-main-jaeger-span-2026-11-24-000001", "jaeger-main-jaeger-span-archive",
          "jaeger-main-jaeger-spans-2026-11-24", "jaeger-main-jaeger-span-20261124", "jaeger-span-2026-11-24",
          "other-jaeger-main-jaeger-span-2026-11-24", "otel-logs-2026-11-24", "top_queries-2026.11.24"]
MAIN = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main", "GITHUB_ACTIONS": "true"}


class Cluster:
    """Answers the kubectl calls the reset makes and records every one of them."""

    def __init__(self, indices=None):
        self.indices = sorted(TRACES + OTHERS if indices is None else indices)
        self.templates = ["jaeger-main-jaeger-service", "jaeger-main-jaeger-span"]
        self.owners = [{"kind": "StatefulSet", "name": "opensearch-cluster-single", "controller": True}]
        self.pod_metadata = {}
        self.containers = ["opensearch"]
        self.cluster_name, self.nodes, self.acknowledged, self.deployment = "opensearch-cluster", 1, True, "jaeger"
        self.listing = None
        self.read_error = False
        self.rollout_error = False
        self.after_delete = lambda name: None
        self.calls, self.limits = [], []

    def answer(self, args, value=None):
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(value) if value is not None else "", stderr="")

    def run(self, args, limit=180):
        self.calls.append(args)
        self.limits.append(limit)
        if args == ["kubectl", "-n", "opensearch", "get", "pod", "opensearch-cluster-single-0", "-o", "json"]:
            return self.answer(args, {"metadata": dict(self.pod_metadata, ownerReferences=self.owners),
                                      "spec": {"containers": [{"name": name} for name in self.containers]}})
        if args == ["kubectl", "-n", "jaeger", "get", "deployment", "jaeger", "-o", "json"]:
            if not self.deployment:
                raise subprocess.CalledProcessError(1, args, stderr="NotFound")
            return self.answer(args, {"metadata": {"name": self.deployment}})
        if args[:4] == ["kubectl", "-n", "jaeger", "rollout"]:
            if self.rollout_error:
                raise subprocess.CalledProcessError(1, args, stderr="rollout failed")
            return self.answer(args)
        prefix = ["kubectl", "-n", "opensearch", "exec", "opensearch-cluster-single-0", "-c", "opensearch", "--",
                  "curl", "-fsS", "--max-time", "60", "-X"]
        assert args[:len(prefix)] == prefix and len(args) == len(prefix) + 2, args
        method, url = args[-2:]
        assert url.startswith("http://localhost:9200/"), url
        path = url[len("http://localhost:9200/"):]
        if method == "DELETE":
            # A pattern or a list would delete more than one index, as the engine would.
            matched = [name for name in self.indices
                       if any(re.fullmatch(part.replace("*", ".*"), name) for part in path.split(","))]
            if not matched:
                raise subprocess.CalledProcessError(22, args, stderr="404")
            self.indices = [name for name in self.indices if name not in matched]
            self.after_delete(path)
            return self.answer(args, {"acknowledged": self.acknowledged})
        assert method == "GET", method
        if self.read_error and path.startswith("_cat/"):
            raise subprocess.CalledProcessError(7, args, stderr="connection refused")
        if path == "":
            return self.answer(args, {"cluster_name": self.cluster_name, "version": {"number": "3.10.0"}})
        if path == "_cluster/health":
            return self.answer(args, {"number_of_nodes": self.nodes, "status": "yellow"})
        if path == "_cat/indices?format=json&h=index&expand_wildcards=all":
            return self.answer(args, self.listing if self.listing is not None
                               else [{"index": name} for name in self.indices])
        if path == "_cat/templates/jaeger*?format=json&h=name":
            return self.answer(args, [{"name": name} for name in self.templates])
        raise AssertionError("unexpected request " + path)

    def deletes(self):
        return [call[-1][len("http://localhost:9200/"):] for call in self.calls if "DELETE" in call]

    def changes(self):
        return [call for call in self.calls if "DELETE" in call or "rollout" in call]


class ResetTests(unittest.TestCase):
    def invoke(self, argv, cluster=None, env=MAIN, verify=None):
        cluster = cluster or Cluster()
        verify = verify or MagicMock()
        out, err = io.StringIO(), io.StringIO()
        with patch.object(reset, "run", cluster.run), patch.object(reset, "verify_ingestion", verify), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = reset.main(argv, dict(env))
        return status, cluster, out.getvalue(), err.getvalue(), verify

    def test_names_are_constants_that_match_the_deployed_configuration(self):
        values = yaml.safe_load((ROOT / "examples/otel-demo/opensearch-values.yaml").read_text())
        self.assertEqual(values["clusterName"], reset.CLUSTER_NAME)
        self.assertEqual(values["clusterName"] + "-" + values["nodeGroup"], reset.STATEFULSET)
        self.assertEqual(reset.STATEFULSET + "-0", reset.POD)
        self.assertEqual(deployer_access.OPENSEARCH_POD, reset.POD)
        self.assertIn("wait_for_statefulset opensearch " + reset.STATEFULSET + " ",
                      (ROOT / "examples/otel-demo/deploy-all.sh").read_text())
        self.assertIn("wait_for_deployment jaeger jaeger ", (ROOT / "examples/otel-demo/deploy-all.sh").read_text())
        self.assertEqual(("opensearch", "jaeger", "jaeger"),
                         (reset.NAMESPACE, reset.JAEGER_NAMESPACE, reset.JAEGER_DEPLOYMENT))
        # The index names come from the main Jaeger backend: its prefix and one index per day.
        backend = yaml.safe_load((ROOT / "examples/otel-demo/jaeger-config.yaml").read_text())[
            "extensions"]["jaeger_storage"]["backends"]["some_storage"]["opensearch"]["indices"]
        self.assertEqual("jaeger-main", backend["index_prefix"])
        self.assertEqual({"spans", "services", "dependencies", "sampling"}, set(backend) - {"index_prefix"})
        for kind in ("spans", "services", "dependencies", "sampling"):
            self.assertEqual(("2006-01-02", "day"), (backend[kind]["date_layout"], backend[kind]["rollover_frequency"]))
        self.assertEqual(("span", "service", "dependencies", "sampling"), reset.FAMILIES)
        # The reset looks for the service that the shared trace check writes.
        self.assertIn('"stringValue": "%s"' % reset.CHECK_SERVICE, (ROOT / "scripts/demo/verify_trace.py").read_text())
        self.assertEqual("delete all Jaeger trace indices in opensearch/opensearch-cluster-single-0",
                         reset.ACKNOWLEDGMENT)

    def test_only_a_daily_jaeger_trace_index_matches(self):
        for name in TRACES:
            self.assertTrue(reset.TRACE_INDEX.fullmatch(name), name)
        for name in OTHERS + ["", "*", "_all", "jaeger-*", "jaeger-main-jaeger-span-*", "jaeger-main-jaeger-span-2026-11-2*",
                              "jaeger-main-jaeger-span-2026-11-24,.kibana_1", "jaeger-main-jaeger-span-2026-11-24\n",
                              "jaeger-main-jaeger-span-2026-11-24/_doc/1", "JAEGER-MAIN-JAEGER-SPAN-2026-11-24",
                              "jaeger-main-jaeger-span-٢٠٢٦-١١-٢٤", "jaeger-main-jaeger-operation-2026-11-24",
                              " jaeger-main-jaeger-span-2026-11-24", "jaeger-main-jaeger-span-2026-11-24?x=1"]:
            self.assertIsNone(reset.TRACE_INDEX.fullmatch(name), repr(name))

    def test_reset_deletes_exactly_the_trace_indices_then_restarts_and_verifies(self):
        with tempfile.NamedTemporaryFile() as summary:
            status, cluster, out, err, verify = self.invoke(
                ["reset", "--acknowledge", reset.ACKNOWLEDGMENT], env=dict(MAIN, GITHUB_STEP_SUMMARY=summary.name))
            self.assertEqual((0, ""), (status, err))
            self.assertIn("Deleted Jaeger trace indices: 6", Path(summary.name).read_text())
        self.assertEqual(sorted(OTHERS), cluster.indices)
        # One request per index, each path an exact name: no pattern, list, alias or query.
        self.assertEqual(sorted(TRACES), cluster.deletes())
        for path in cluster.deletes():
            self.assertRegex(path, r"\A[a-z0-9-]+\Z")
        # Every check precedes the first change, and the restart and the trace check follow the last delete.
        changes = [index for index, call in enumerate(cluster.calls) if "DELETE" in call or "rollout" in call]
        reads = [index for index, call in enumerate(cluster.calls)
                 if call[3] == "get" or call[-1].endswith(("9200/", "_cluster/health"))]
        self.assertLess(max(reads), min(changes))
        self.assertEqual([["kubectl", "-n", "jaeger", "rollout", "restart", "deployment/jaeger"],
                          ["kubectl", "-n", "jaeger", "rollout", "status", "deployment/jaeger", "--timeout=600s"]],
                         cluster.changes()[-2:])
        # The process limit outlasts the rollout wait, so kubectl reports a slow rollout itself.
        self.assertEqual(660, cluster.limits[-1])
        self.assertEqual({180}, set(cluster.limits[:-1]))
        verify.assert_called_once_with()
        self.assertIn("Reset complete: 6 Jaeger trace index(es) deleted", out)
        for name in OTHERS:
            self.assertIn(name, out)
        # No Kubernetes object is deleted, created or patched other than the one restart.
        for call in cluster.calls:
            self.assertNotIn(call[3], ("delete", "create", "patch", "apply", "scale", "replace"))

    def test_the_acknowledgment_must_match_exactly_before_any_cluster_call(self):
        phrase = reset.ACKNOWLEDGMENT
        for typed in ("", "yes", phrase.lower(), phrase.upper(), phrase + " ", " " + phrase, phrase + "\n",
                      '"%s"' % phrase, phrase.replace("all ", ""), phrase.replace("-0", "-1"),
                      phrase.replace("opensearch/", "jaeger/"), phrase + " and .kibana_1"):
            for argv in (["reset", "--acknowledge", typed], ["reset", "--acknowledge=" + typed, "--validate-only"]):
                with self.subTest(typed=typed, argv=argv):
                    status, cluster, _, err, verify = self.invoke(argv)
                    self.assertEqual(2, status)
                    self.assertEqual([], cluster.calls)
                    verify.assert_not_called()
                    self.assertIn("Refused, nothing was deleted", err)
        status, cluster, _, _, _ = self.invoke(["reset"])
        self.assertEqual((2, []), (status, cluster.calls))

    def test_only_a_manual_dispatch_from_main_may_run(self):
        accepted = ["reset", "--acknowledge", reset.ACKNOWLEDGMENT]
        for env in ({"GITHUB_EVENT_NAME": "schedule", "GITHUB_REF": "refs/heads/main"},
                    {"GITHUB_EVENT_NAME": "push", "GITHUB_REF": "refs/heads/main"},
                    {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_REF": "refs/pull/1/merge"},
                    {"GITHUB_EVENT_NAME": "workflow_run", "GITHUB_REF": "refs/heads/main"},
                    {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/topic"},
                    {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/tags/main"},
                    {"GITHUB_EVENT_NAME": "workflow_dispatch"}):
            for argv in (accepted, ["list"], accepted + ["--validate-only"]):
                with self.subTest(env=env, argv=argv):
                    status, cluster, _, _, verify = self.invoke(argv, env=env)
                    self.assertEqual((2, []), (status, cluster.calls))
                    verify.assert_not_called()
        # An administrator's shell has no event, and still needs the phrase.
        self.assertEqual(0, self.invoke(accepted, env={})[0])
        self.assertEqual(2, self.invoke(["reset", "--acknowledge", "yes"], env={})[0])

    def test_validate_only_accepts_a_good_request_without_touching_the_cluster(self):
        for argv in (["list", "--validate-only"], ["reset", "--acknowledge", reset.ACKNOWLEDGMENT, "--validate-only"]):
            status, cluster, out, _, verify = self.invoke(argv)
            self.assertEqual((0, []), (status, cluster.calls))
            verify.assert_not_called()
            self.assertIn("request is accepted", out)

    def test_no_argument_or_environment_variable_selects_a_resource(self):
        for argv in (["reset", "--namespace", "default"], ["reset", "--pod", "other-0"], ["reset", "--index", "*"],
                     ["reset", ".kibana_1"], ["delete"], ["reset", "--acknowledge", reset.ACKNOWLEDGMENT, "extra"], []):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as stopped, \
                    contextlib.redirect_stderr(io.StringIO()):
                reset.main(argv, dict(MAIN))
            self.assertEqual(2, stopped.exception.code)
        source = (ROOT / "scripts/demo/opensearch_reset.py").read_text()
        self.assertEqual({"GITHUB_EVENT_NAME", "GITHUB_REF", "GITHUB_STEP_SUMMARY"},
                         set(re.findall(r'env(?:\.get\(|\[)"([A-Z_]+)"', source)))
        self.assertEqual(1, source.count("os.environ"))
        # A hostile environment changes no name.
        hostile = dict(MAIN, NAMESPACE="default", POD="other-0", OPENSEARCH_RESET_INDEX="*", KUBECTL_NAMESPACE="default")
        status, cluster, _, _, _ = self.invoke(["reset", "--acknowledge", reset.ACKNOWLEDGMENT], env=hostile)
        self.assertEqual((0, sorted(OTHERS)), (status, cluster.indices))

    def test_list_changes_nothing(self):
        status, cluster, out, _, verify = self.invoke(["list"])
        self.assertEqual(0, status)
        self.assertEqual([], cluster.changes())
        self.assertEqual(sorted(TRACES + OTHERS), cluster.indices)
        verify.assert_not_called()
        self.assertIn("A reset would delete 6 Jaeger trace index(es):\n" + "\n".join(sorted(TRACES)), out)

    def test_an_unexpected_pod_engine_or_listing_is_refused_before_any_change(self):
        def foreign_owner(cluster):
            cluster.owners = [{"kind": "ReplicaSet", "name": "opensearch-cluster-single", "controller": True}]

        def renamed_owner(cluster):
            cluster.owners = [{"kind": "StatefulSet", "name": "other", "controller": True}]

        def two_owners(cluster):
            cluster.owners = cluster.owners * 2

        cases = {
            "foreign owner": foreign_owner, "renamed owner": renamed_owner, "two owners": two_owners,
            "no owner": lambda cluster: setattr(cluster, "owners", []),
            "terminating": lambda cluster: setattr(cluster, "pod_metadata", {"deletionTimestamp": "now"}),
            "no container": lambda cluster: setattr(cluster, "containers", ["sidecar"]),
            "other engine": lambda cluster: setattr(cluster, "cluster_name", "docker-cluster"),
            "two nodes": lambda cluster: setattr(cluster, "nodes", 2),
            "not a list": lambda cluster: setattr(cluster, "listing", {"error": "x"}),
            "no name": lambda cluster: setattr(cluster, "listing", [{"index": TRACES[0]}, {"health": "green"}]),
            "empty name": lambda cluster: setattr(cluster, "listing", [{"index": ""}]),
            "not text": lambda cluster: setattr(cluster, "listing", [{"index": ["*"]}]),
            "twice": lambda cluster: setattr(cluster, "listing", [{"index": TRACES[0]}] * 2),
        }
        for name, change in cases.items():
            for operation in (["list"], ["reset", "--acknowledge", reset.ACKNOWLEDGMENT]):
                with self.subTest(case=name, operation=operation[0]):
                    cluster = Cluster()
                    change(cluster)
                    status, cluster, _, err, verify = self.invoke(operation, cluster)
                    self.assertEqual(2, status, err)
                    self.assertEqual([], cluster.changes())
                    self.assertEqual(sorted(TRACES + OTHERS), cluster.indices)
                    verify.assert_not_called()
        # A Jaeger Deployment that cannot be read fails the run before any delete.
        cluster = Cluster()
        cluster.deployment = ""
        status, cluster, _, _, _ = self.invoke(["reset", "--acknowledge", reset.ACKNOWLEDGMENT], cluster)
        self.assertEqual((1, []), (status, cluster.changes()))

    def test_a_listing_of_patterns_or_lists_deletes_nothing(self):
        # Names that are not daily trace indices are kept, whatever they would expand to.
        cluster = Cluster(indices=OTHERS)
        cluster.listing = [{"index": name} for name in ("*", "_all", "jaeger-*", "jaeger-main-jaeger-span-*",
                                                        TRACES[0] + "," + OTHERS[0], "-" + TRACES[0])]
        status, cluster, _, _, _ = self.invoke(["reset", "--acknowledge", reset.ACKNOWLEDGMENT], cluster)
        self.assertEqual(0, status)
        self.assertEqual([], cluster.deletes())
        self.assertEqual(sorted(OTHERS), cluster.indices)

    def test_an_unacknowledged_or_failed_delete_stops_the_run(self):
        cluster = Cluster()
        cluster.acknowledged = False
        status, cluster, _, err, verify = self.invoke(["reset", "--acknowledge", reset.ACKNOWLEDGMENT], cluster)
        self.assertEqual(1, status)
        self.assertEqual(1, len(cluster.deletes()))
        self.assertIn("did not acknowledge", err)
        verify.assert_not_called()

        cluster = Cluster()
        # Something else removes an index between the listing and its delete.
        cluster.after_delete = lambda name: cluster.indices.remove(TRACES[1]) if name == TRACES[0] else None
        status, cluster, _, err, _ = self.invoke(["reset", "--acknowledge", reset.ACKNOWLEDGMENT], cluster)
        self.assertEqual(1, status)
        self.assertIn("Failed", err)
        self.assertTrue(set(OTHERS) <= set(cluster.indices))

    def test_a_reset_that_fails_after_a_delete_still_restarts_jaeger(self):
        restart = ["kubectl", "-n", "jaeger", "rollout", "restart", "deployment/jaeger"]

        def unacknowledged(cluster):
            cluster.acknowledged = False

        def removed_by_something_else(cluster):
            cluster.after_delete = lambda name: cluster.indices.remove(TRACES[1]) if name == TRACES[0] else None

        def unreadable_afterwards(cluster):
            cluster.after_delete = lambda name: setattr(cluster, "read_error", True) if name == TRACES[-1] else None

        for change, message, deletes in ((unacknowledged, "did not acknowledge", 1),
                                         (removed_by_something_else, "Failed", 2),
                                         (unreadable_afterwards, "connection refused", len(TRACES))):
            with self.subTest(change=change.__name__):
                cluster = Cluster()
                change(cluster)
                status, cluster, _, err, verify = self.invoke(["reset", "--acknowledge", reset.ACKNOWLEDGMENT], cluster)
                self.assertEqual(1, status)
                self.assertEqual(deletes, len(cluster.deletes()))
                # Jaeger forgets the services it cached, and the error that stopped the reset is still the one reported.
                self.assertEqual([restart, ["kubectl", "-n", "jaeger", "rollout", "status", "deployment/jaeger",
                                            "--timeout=600s"]], [call for call in cluster.changes() if "rollout" in call])
                self.assertIn("Restarted jaeger/jaeger after the failed reset", err)
                self.assertIn(message, err)
                verify.assert_not_called()

    def test_a_failed_restart_after_a_partial_reset_keeps_the_original_error(self):
        cluster = Cluster()
        cluster.acknowledged = False
        cluster.rollout_error = True
        status, cluster, _, err, verify = self.invoke(["reset", "--acknowledge", reset.ACKNOWLEDGMENT], cluster)
        self.assertEqual(1, status)
        self.assertIn("Jaeger could not be restarted after the failed reset; restart jaeger/jaeger by hand", err)
        self.assertIn("did not acknowledge", err.splitlines()[-1])
        verify.assert_not_called()

    def test_a_refusal_before_any_delete_restarts_nothing(self):
        cluster = Cluster()
        cluster.read_error = True
        status, cluster, _, err, _ = self.invoke(["reset", "--acknowledge", reset.ACKNOWLEDGMENT], cluster)
        self.assertEqual(1, status)
        self.assertEqual([], cluster.changes())
        self.assertNotIn("Restarted", err)

    def test_trace_indices_recreated_by_ingestion_are_expected(self):
        cluster = Cluster()
        cluster.after_delete = lambda name: cluster.indices.append(name) if name == TRACES[-1] else None
        status, cluster, _, err, verify = self.invoke(["reset", "--acknowledge", reset.ACKNOWLEDGMENT], cluster)
        self.assertEqual((0, ""), (status, err))
        verify.assert_called_once_with()

    def test_another_index_or_a_template_that_disappears_fails_the_run_after_jaeger_is_restarted(self):
        def lose_index(cluster):
            cluster.after_delete = lambda name: (OTHERS[0] in cluster.indices and cluster.indices.remove(OTHERS[0]))

        def lose_template(cluster):
            cluster.after_delete = lambda name: cluster.templates.clear()

        for change, message in ((lose_index, "Indices outside the reset are gone"),
                                (lose_template, "index templates changed")):
            with self.subTest(message=message):
                cluster = Cluster()
                change(cluster)
                status, cluster, _, err, verify = self.invoke(["reset", "--acknowledge", reset.ACKNOWLEDGMENT], cluster)
                self.assertEqual(1, status)
                self.assertIn(message, err)
                # The trace indices are gone by then, so Jaeger is still restarted and checked.
                self.assertIn(["kubectl", "-n", "jaeger", "rollout", "restart", "deployment/jaeger"], cluster.calls)
                verify.assert_called_once_with()

    def test_a_failed_restart_or_trace_check_fails_the_run(self):
        status, _, _, err, _ = self.invoke(["reset", "--acknowledge", reset.ACKNOWLEDGMENT],
                                           verify=MagicMock(side_effect=RuntimeError("no fresh trace")))
        self.assertEqual(1, status)
        self.assertIn("no fresh trace", err)

    def test_ingestion_check_forwards_both_services_and_finds_the_fresh_service(self):
        started = []

        def popen(args, **kwargs):
            process = MagicMock()
            process.__enter__.return_value = process
            started.append((args, process))
            return process

        services = iter([urllib.error.URLError("starting"), {"services": ["frontend"]},
                         {"services": ["frontend", reset.CHECK_SERVICE]}])

        def request(url, body=None):
            self.assertEqual("http://127.0.0.1:16687/api/v3/services", url)
            answer = next(services)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with patch.object(reset.verify_trace, "check_trace") as check, \
                patch.object(reset.verify_trace, "request", request), contextlib.redirect_stdout(io.StringIO()):
            reset.verify_ingestion(popen=popen, pause=0)
        check.assert_called_once_with("http://127.0.0.1:14318", "http://127.0.0.1:16687")
        self.assertEqual([["kubectl", "-n", "jaeger", "port-forward", "svc/jaeger-collector", "14318:4318"],
                          ["kubectl", "-n", "jaeger", "port-forward", "svc/jaeger-query", "16687:16686"]],
                         [args for args, _ in started])
        for _, process in started:
            process.terminate.assert_called_once_with()

        with patch.object(reset.verify_trace, "check_trace"), \
                patch.object(reset.verify_trace, "request", return_value={"services": ["frontend"]}), \
                self.assertRaisesRegex(RuntimeError, "does not list the service"):
            reset.verify_ingestion(popen=popen, attempts=3, pause=0)

    def test_the_committed_manual_role_already_allows_every_request_of_the_reset(self):
        roles = deployer_access.load_roles()
        request = deployer_access.Request
        for needed in (request("opensearch", "", "pods", "get", reset.POD),
                       request("opensearch", "", "pods/exec", "get", reset.POD),
                       request("opensearch", "", "pods/exec", "create", reset.POD),
                       request("jaeger", "apps", "deployments", "get", "jaeger"),
                       request("jaeger", "apps", "deployments", "patch", "jaeger"),
                       request("jaeger", "apps", "deployments", "watch", ""),
                       request("jaeger", "", "pods/portforward", "create", "")):
            with self.subTest(request=needed):
                self.assertTrue(deployer_access.allowed("manual", needed, roles))
        # It deletes no Kubernetes object, so the role gains nothing for it.
        for forbidden in (request("opensearch", "", "persistentvolumeclaims", "delete", "data"),
                          request("opensearch", "", "pods", "delete", reset.POD),
                          request("opensearch", "apps", "statefulsets/scale", "patch", reset.STATEFULSET)):
            self.assertFalse(deployer_access.allowed("manual", forbidden, roles))


if __name__ == "__main__":
    unittest.main()
