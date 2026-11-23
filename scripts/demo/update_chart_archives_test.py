# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Offline tests for scripts/utils/update-chart-archives.py using fixture archives and indexes."""

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

import yaml

import deploy
import chart_archives as inputs

SPEC = importlib.util.spec_from_file_location("update_chart_archives", deploy.ROOT / "scripts/utils/update-chart-archives.py")
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)

VERSIONS = {"OPENSEARCH_CHART_VERSION": "3.7.0", "OPENSEARCH_DASHBOARDS_CHART_VERSION": "3.7.1",
            "OTEL_DEMO_CHART_VERSION": "0.40.9", "PROMETHEUS_STACK_CHART_VERSION": "82.10.4"}
JAEGER = {"cassandra-0.15.3.tgz": "a" * 64}


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Fixture:
    """A fake checkout, publisher indexes and archives; getter() never touches the network."""

    def __init__(self, temp, versions=VERSIONS):
        self.root = Path(temp)
        self.files = {}
        otel = "".join(f'{k}="{v}"\n' for k, v in versions.items() if k != "PROMETHEUS_STACK_CHART_VERSION")
        for base, text in (("examples/otel-demo", otel),
                           ("examples/oci", 'PROMETHEUS_STACK_CHART_VERSION="${PROMETHEUS_STACK_CHART_VERSION:-%s}"\n'
                            % versions["PROMETHEUS_STACK_CHART_VERSION"])):
            (self.root / base).mkdir(parents=True)
            (self.root / base / "deploy-all.sh").write_text(text)
        (self.root / "scripts/demo").mkdir(parents=True)
        self.entries = {}
        for _, variable, chart, repository in inputs.CHARTS:
            self.publish(repository, chart, versions[variable])
        self.write_indexes()

    def publish(self, repository, chart, version, body=None, digest=None):
        name = f"{chart}-{version}.tgz"
        body = body if body is not None else f"archive {name}".encode()
        self.files[f"{repository}/{name}"] = body
        self.entries.setdefault(repository, {}).setdefault(chart, []).append(
            {"version": version, "digest": digest or sha(body), "urls": [name]})

    def write_indexes(self):
        for repository, entries in self.entries.items():
            self.files[repository + "/index.yaml"] = yaml.safe_dump({"apiVersion": "v1", "entries": entries}).encode()

    def write_lock(self, charts):
        (self.root / inputs.LOCK).write_text(json.dumps({"charts": charts, "jaegerDependencies": JAEGER}, indent=2) + "\n")

    def lock_text(self):
        return (self.root / inputs.LOCK).read_text()

    def good_charts(self):
        return {f"{c}-{VERSIONS[v]}.tgz": sha(f"archive {c}-{VERSIONS[v]}.tgz".encode()) for _, v, c, _ in inputs.CHARTS}

    def getter(self, url):
        return self.files[url]

    def run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = helper.main(list(argv), root=self.root, getter=self.getter)
        return code, out.getvalue(), err.getvalue()


class ChartArchiveHelperTests(unittest.TestCase):
    def test_rewrites_only_the_charts_map_and_keeps_jaeger_dependencies(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(temp)
            f.write_lock({"opensearch-3.6.0.tgz": "b" * 64})
            code, out, _ = f.run()
            self.assertEqual(code, 0)
            lock = json.loads(f.lock_text())
            self.assertEqual(lock["charts"], dict(sorted(f.good_charts().items())))
            self.assertEqual(len(lock["charts"]), 4)
            self.assertEqual(lock["jaegerDependencies"], JAEGER)
            self.assertIn("updated", out)
            self.assertTrue(f.lock_text().endswith("}\n"))

    def test_matching_lock_is_a_byte_identical_no_op(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(temp)
            f.write_lock(dict(sorted(f.good_charts().items())))
            before = f.lock_text()
            self.assertEqual(f.run()[0], 0)
            self.assertEqual(f.run("--check")[0], 0)
            self.assertEqual(f.lock_text(), before)

    def test_check_changes_nothing_and_reports_every_difference(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(temp)
            stale = dict(f.good_charts())
            stale["opensearch-3.6.0.tgz"] = stale.pop("opensearch-3.7.0.tgz")
            stale["opentelemetry-demo-0.40.9.tgz"] = "c" * 64
            f.write_lock(stale)
            before = f.lock_text()
            code, out, err = f.run("--check")
            self.assertEqual(code, 1)
            self.assertEqual(f.lock_text(), before)
            self.assertIn("remove opensearch-3.6.0.tgz", out)
            self.assertIn("add opensearch-3.7.0.tgz", out)
            self.assertIn("change opentelemetry-demo-0.40.9.tgz", out)
            self.assertIn("python3 scripts/utils/update-chart-archives.py", err)

    def test_archive_that_differs_from_the_index_digest_is_refused_and_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(temp)
            repository = next(r for _, v, c, r in inputs.CHARTS if c == "opensearch")
            f.entries[repository]["opensearch"] = []
            f.publish(repository, "opensearch", "3.7.0", digest="d" * 64)
            f.write_indexes()
            f.write_lock({"stale.tgz": "e" * 64})
            before = f.lock_text()
            for argv in ((), ("--check",)):
                code, _, err = f.run(*argv)
                self.assertEqual(code, 1)
                self.assertIn("differs from the index.yaml digest", err)
            self.assertEqual(f.lock_text(), before)

    def test_missing_or_duplicate_index_entries_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(temp)
            repository = next(r for _, v, c, r in inputs.CHARTS if c == "opensearch")
            f.entries[repository]["opensearch"] = []
            f.write_indexes()
            f.write_lock({})
            self.assertEqual(f.run()[0], 1)
            f.publish(repository, "opensearch", "3.7.0")
            f.publish(repository, "opensearch", "3.7.0")
            f.write_indexes()
            code, _, err = f.run()
            self.assertEqual(code, 1)
            self.assertIn("found 2", err)

    def test_non_https_archive_urls_are_refused(self):
        with self.assertRaises(ValueError):
            helper.archive_url("https://example.test", {"urls": ["http://example.test/x.tgz"]}, "x.tgz")
        self.assertEqual(helper.archive_url("https://example.test/charts", {"urls": ["x-1.tgz"]}, "x-1.tgz"),
                         "https://example.test/charts/x-1.tgz")

    def test_redirects_and_initial_urls_must_be_https(self):
        handler = helper.HttpsOnlyRedirects()
        request = helper.urllib.request.Request("https://example.test/index.yaml")
        with self.assertRaisesRegex(ValueError, "not HTTPS"):
            handler.redirect_request(request, io.BytesIO(), 302, "Found", {}, "http://example.test/index.yaml")
        followed = handler.redirect_request(request, io.BytesIO(), 302, "Found", {}, "https://cdn.example.test/index.yaml")
        self.assertEqual(followed.full_url, "https://cdn.example.test/index.yaml")
        with self.assertRaisesRegex(ValueError, "not HTTPS"):
            helper.fetch("http://example.test/chart.tgz")
        with self.assertRaisesRegex(ValueError, "not HTTPS"):
            helper.load_index("http://example.test", lambda url: b"")

    def test_committed_lock_has_the_shape_the_helper_writes(self):
        lock = json.loads((deploy.ROOT / inputs.LOCK).read_text())
        text = json.dumps({"charts": dict(sorted(lock["charts"].items())), "jaegerDependencies": lock["jaegerDependencies"]},
                          indent=2) + "\n"
        self.assertEqual((deploy.ROOT / inputs.LOCK).read_text(), text)


if __name__ == "__main__":
    unittest.main()
