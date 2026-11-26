# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import importlib.util
from pathlib import Path
import subprocess
import unittest

SPEC = importlib.util.spec_from_file_location(
    "helmfile_runbook_rehearsal", Path(__file__).resolve().parent / "rehearsals/helmfile-runbook/exercise.py")
exercise = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(exercise)


def deployment(generation=3, observed=3, terminating=None):
    status = {"observedGeneration": observed}
    if terminating is not None:
        status["terminatingReplicas"] = terminating
    return {"metadata": {"generation": generation}, "status": status}


class Clock:
    """A clock that only sleeps move."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class SettleGeneratorsTest(unittest.TestCase):
    def settle(self, states, timeout=120):
        """Run settle_generators over successive {namespace: Deployment} states, one per poll."""
        clock = Clock()
        polls = iter(states)
        reads = []
        current = {}

        def read(ns, seconds):
            if not reads or reads[-1][0] == "jaeger":
                current.update(next(polls))
            reads.append((ns, seconds))
            return current[ns]

        exercise.settle_generators(read, timeout=timeout, clock=clock, sleep=clock.sleep)
        return reads, clock

    def test_reads_both_generator_namespaces(self):
        self.assertEqual(sorted(exercise.helmfile_deploy.GENERATOR.values()), ["default", "jaeger"])

    def test_returns_at_once_when_both_generators_are_settled(self):
        reads, clock = self.settle([{"default": deployment(), "jaeger": deployment(terminating=0)}])
        self.assertEqual([ns for ns, _ in reads], ["default", "jaeger"])
        self.assertEqual(clock.sleeps, [])

    def test_waits_while_an_old_generator_pod_terminates(self):
        reads, clock = self.settle([
            {"default": deployment(terminating=1), "jaeger": deployment()},
            {"default": deployment(terminating=1), "jaeger": deployment()},
            {"default": deployment(terminating=0), "jaeger": deployment()},
        ])
        self.assertEqual(len(reads), 6)
        self.assertEqual(clock.sleeps, [2, 2])

    def test_waits_for_an_unobserved_generation_in_either_namespace(self):
        reads, clock = self.settle([
            {"default": deployment(), "jaeger": deployment(generation=4, observed=3)},
            {"default": deployment(), "jaeger": deployment(generation=4, observed=4)},
        ])
        self.assertEqual(clock.sleeps, [2])

    def test_a_deployment_without_status_is_not_settled(self):
        self.assertEqual(exercise.unsettled_generators({"default": {"metadata": {"generation": 1}}}), ["default"])

    def test_stops_with_the_unsettled_namespace_at_the_limit(self):
        with self.assertRaisesRegex(AssertionError, r"did not settle within 10s: \['default'\]"):
            self.settle([{"default": deployment(terminating=1), "jaeger": deployment()}] * 10, timeout=10)

    def test_bounds_each_read_by_the_time_left_and_names_a_stalled_one(self):
        clock = Clock()
        budgets = []

        def read(ns, seconds):
            budgets.append(seconds)
            if ns == "jaeger":
                raise subprocess.TimeoutExpired(["kubectl"], seconds)
            clock.now += 7
            return deployment()

        with self.assertRaisesRegex(AssertionError, "Reading the jaeger trace generator passed the 30s settle limit"):
            exercise.settle_generators(read, timeout=30, clock=clock, sleep=clock.sleep)
        self.assertEqual(budgets, [30, 23])


class ScriptsEngineGuardTest(unittest.TestCase):
    """The walk keeps its kind demonstration of the scripts engine's companion guard (#158)."""

    def test_the_walk_runs_the_scripts_engine_around_adoption_and_de_adoption(self):
        source = Path(exercise.__file__).read_text()
        walk = source[source.index("def exercise("):]
        # It calls the engine's own entry point and replaces only the stack script.
        self.assertIn("deploy.reconcile(env)", walk)
        self.assertIn('mock.patch.object(deploy, "deploy_component"', walk)
        self.assertNotIn('mock.patch.object(deploy, "companion_releases"', walk)
        self.assertNotIn('mock.patch.object(deploy, "refuse_companion_releases"', walk)
        # A refusal is the guard's own error, after release reads only and with the record unchanged.
        refused = walk[walk.index("def scripts_engine_refused("):walk.index("# 1. Operator shell.")]
        for text in ("isinstance(error, deploy.CompanionReleaseError)", '["helm", "list"], ["helm", "history"]',
                     "not deployed and fingerprints() == before"):
            self.assertIn(text, refused)
        # Before adoption, after the first install, with a failed install, with all four adopted on
        # both events, before each namespace's de-adoption, and after the last one.
        order = [walk.index(text) for text in (
            "# Before adoption the scripts engine runs",
            "if ns == ORDER[0]:\n            # From the first companion install on",
            "for event in (\"workflow_dispatch\", \"schedule\"):\n        scripts_engine_refused(held, event)",
            "for ns in reversed(ORDER):\n        # Each namespace that still holds its release records",
            "# De-adoption re-enables the scripts engine",
            '"scriptsEngineRefusedWhileACompanionExists": True, "scriptsEngineRanAfterDeAdoption": True')]
        self.assertEqual(order, sorted(order))
        self.assertIn("# A failed first install is a companion release too.\n            scripts_engine_refused(", walk)
        self.assertEqual(walk.count("error is None and deployed == [\"oci-monitoring\"]"), 2)


if __name__ == "__main__":
    unittest.main()
