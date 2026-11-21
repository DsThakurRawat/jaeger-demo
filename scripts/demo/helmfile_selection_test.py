# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

from copy import deepcopy
import json
import re
import unittest

import yaml

from helmfile_selection import selection, STATE


class HelmfileSelectionTest(unittest.TestCase):
    def setUp(self):
        self.state = yaml.safe_load(STATE.read_text())
        self.releases = self.state["releases"]

    def test_six_application_identities_and_labels(self):
        actual = {(r["namespace"], r["name"]): (r["labels"]["stack"], r["labels"]["component"])
                  for r in self.releases}
        self.assertEqual(actual, {
            ("default", "jaeger"): ("oci", "jaeger"),
            ("default", "prometheus"): ("oci", "monitoring"),
            ("jaeger", "jaeger"): ("otel", "jaeger"),
            ("opensearch", "opensearch"): ("otel", "opensearch"),
            ("opensearch", "opensearch-dashboards"): ("otel", "opensearch"),
            ("otel-demo", "otel-demo"): ("otel", "otel-demo"),
        })
        self.assertEqual(len(actual), len(self.releases))

    def test_prerequisites_follow_selected_stack_and_component(self):
        self.assertEqual(selection("oci", "monitoring", self.releases), ["default/prometheus"])
        self.assertEqual(selection("oci", "jaeger", self.releases), ["default/prometheus", "default/jaeger"])
        self.assertEqual(selection("otel", "opensearch", self.releases), [
            "opensearch/opensearch", "opensearch/opensearch-dashboards"])
        self.assertEqual(selection("otel", "otel-demo", self.releases), [
            "opensearch/opensearch", "jaeger/jaeger", "otel-demo/otel-demo"])
        self.assertEqual(len(selection("otel", "all", self.releases)), 4)

    def test_invalid_component_duplicate_and_foreign_prerequisite_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "No monitoring release"):
            selection("otel", "monitoring", self.releases)
        with self.assertRaisesRegex(ValueError, "Duplicate release"):
            selection("oci", "jaeger", self.releases + [self.releases[0]])
        changed = deepcopy(self.releases)
        changed[0]["needs"] = ["opensearch/opensearch"]
        with self.assertRaisesRegex(ValueError, "cross-stack prerequisite"):
            selection("oci", "monitoring", changed)
        changed[0]["needs"] = ["default/jaeger"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            selection("oci", "monitoring", changed)

    def test_every_repository_chart_pin_has_a_reviewed_archive_checksum(self):
        # Bundle verification refuses a chart without one. Fail here, in CI, and not at build time.
        reviewed = json.loads((STATE.parent / "ops/executor/chart-archives.json").read_text())["charts"]
        pinned = [r for r in self.releases if "version" in r]
        self.assertEqual(4, len(pinned))
        for release in pinned:
            archive = release["chart"].rpartition("/")[2] + "-" + str(release["version"]) + ".tgz"
            with self.subTest(archive=archive):
                self.assertRegex(reviewed.get(archive, ""), re.compile(r"^[a-f0-9]{64}$"))

    def test_open_search_and_namespaces_remain_in_place(self):
        self.assertFalse(self.state["helmDefaults"]["createNamespace"])
        for release in self.releases:
            if release["labels"]["component"] == "opensearch":
                self.assertFalse(release.get("atomic", self.state["helmDefaults"].get("atomic", False)))
                self.assertNotIn("force", release)
