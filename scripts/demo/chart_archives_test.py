# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import contextlib
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import chart_archives


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Fixture:
    """A temporary root with a reviewed lock and a fake Helm that records its argv."""

    def __init__(self, temp, body=b"reviewed chart"):
        self.root = Path(temp) / "root"
        (self.root / "scripts/demo").mkdir(parents=True)
        self.destination = Path(temp) / "archives"
        self.destination.mkdir()
        self.lock = {"charts": {"opensearch-3.10.0.tgz": sha(b"reviewed chart")},
                     "jaegerDependencies": {"common-2.19.1.tgz": sha(b"common"), "kafka-26.6.2.tgz": sha(b"kafka")}}
        (self.root / chart_archives.LOCK).write_text(json.dumps(self.lock))
        self.body = body
        self.calls = []

    def run(self, args, **kwargs):
        self.calls.append(args)
        folder = Path(args[args.index("--destination") + 1])
        (folder / f"{args[2]}-{args[args.index('--version') + 1]}.tgz").write_bytes(self.body)
        return subprocess.CompletedProcess(args, 0)

    def pull(self, chart="opensearch", version="3.10.0"):
        return chart_archives.pull(chart, version, self.destination, root=self.root, run=self.run)


class ChartArchiveTests(unittest.TestCase):
    def test_checked_archives_reject_changed_bytes_and_set_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "chart-1.0.0.tgz"
            path.write_bytes(b"reviewed")
            lock = {path.name: sha(b"reviewed")}
            self.assertEqual({path.name: b"reviewed"}, chart_archives.checked_archives([path], lock))
            path.write_bytes(b"different")
            with self.assertRaisesRegex(ValueError, "differs from approved SHA-256: chart-1.0.0.tgz"):
                chart_archives.checked_archives([path], lock)
            with self.assertRaisesRegex(ValueError, "set differs"):
                chart_archives.checked_archives([], lock)
            extra = Path(temp) / "extra-1.0.0.tgz"
            extra.write_bytes(b"reviewed")
            path.write_bytes(b"reviewed")
            with self.assertRaisesRegex(ValueError, "set differs"):
                chart_archives.checked_archives([path, extra], lock)

    def test_deploy_script_pins_have_exact_matching_lock_entries(self):
        lock = chart_archives.load_lock()
        pinned = {f"{chart}-{chart_archives.pin(chart_archives.ROOT, variable)}.tgz"
                  for _, variable, chart, _ in chart_archives.CHARTS}
        self.assertEqual(4, len(pinned))
        self.assertEqual(pinned, set(lock["charts"]),
                         "scripts/demo/chart-archives.lock.json does not match the chart pins in the deploy-all.sh scripts. "
                         "Run: python3 scripts/utils/update-chart-archives.py")

    def test_pull_uses_the_publisher_repository_and_exact_version(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(temp)
            path = fixture.pull()
            self.assertEqual(b"reviewed chart", path.read_bytes())
            self.assertEqual("opensearch-3.10.0.tgz", path.name)
            self.assertEqual(fixture.destination, path.parent.parent)
            self.assertEqual([["helm", "pull", "opensearch", "--version", "3.10.0",
                               "--repo", "https://opensearch-project.github.io/helm-charts",
                               "--destination", str(path.parent)]], fixture.calls)

    def test_pull_refuses_a_digest_mismatch_and_removes_the_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(temp, body=b"replaced chart")
            with self.assertRaisesRegex(ValueError, "differs from approved SHA-256: opensearch-3.10.0.tgz"):
                fixture.pull()
            self.assertEqual([], [p for p in fixture.destination.rglob("*") if p.is_file()])

    def test_pull_refuses_unlocked_versions_and_charts_before_downloading(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(temp)
            for chart, version, message in (("opensearch", "3.11.0", "no reviewed SHA-256"),
                                            ("opensearch-dashboards", "3.10.0", "no reviewed SHA-256"),
                                            ("opensearch", "latest", "exact chart version"),
                                            ("opensearch", "^3.10.0", "exact chart version"),
                                            ("prometheus-community/kube-prometheus-stack", "82.10.4", "Unknown chart")):
                with self.subTest(chart=chart, version=version):
                    with self.assertRaisesRegex(ValueError, message):
                        fixture.pull(chart, version)
            self.assertEqual([], fixture.calls)

    def test_pull_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(temp)
            with self.assertRaisesRegex(ValueError, "helm pull opensearch 3.10.0 failed"):
                chart_archives.pull("opensearch", "3.10.0", fixture.destination, root=fixture.root,
                                    run=lambda args, **kwargs: subprocess.CompletedProcess(args, 1))

    def test_cli_prints_only_a_verified_path(self):
        for body, code in ((b"reviewed chart", 0), (b"replaced chart", 1)):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as temp:
                fixture = Fixture(temp, body=body)
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(chart_archives, "ROOT", fixture.root), \
                        mock.patch.object(chart_archives.subprocess, "run", fixture.run), \
                        contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    result = chart_archives.main(["pull", "opensearch", "3.10.0", str(fixture.destination)])
                self.assertEqual(code, result)
                if code:
                    self.assertEqual("", stdout.getvalue())
                    self.assertIn("differs from approved SHA-256", stderr.getvalue())
                else:
                    path = Path(stdout.getvalue().strip())
                    self.assertEqual(b"reviewed chart", path.read_bytes())
        self.assertEqual(2, chart_archives.main(["pull", "opensearch"]))

    def test_dependencies_must_equal_the_locked_set(self):
        with tempfile.TemporaryDirectory() as temp:
            fixture = Fixture(temp)
            charts = Path(temp) / "charts"
            charts.mkdir()
            (charts / "common-2.19.1.tgz").write_bytes(b"common")
            (charts / "kafka-26.6.2.tgz").write_bytes(b"kafka")

            def check():
                with mock.patch.object(chart_archives, "ROOT", fixture.root), \
                        contextlib.redirect_stderr(io.StringIO()) as stderr:
                    return chart_archives.main(["dependencies", str(charts)]), stderr.getvalue()

            self.assertEqual((0, ""), check())
            (charts / "kafka-26.6.2.tgz").write_bytes(b"other kafka")
            self.assertEqual(1, check()[0])
            self.assertIn("kafka-26.6.2.tgz", check()[1])
            (charts / "kafka-26.6.2.tgz").write_bytes(b"kafka")
            (charts / "cassandra-0.15.3.tgz").write_bytes(b"cassandra")
            self.assertEqual(1, check()[0])
            self.assertIn("set differs", check()[1])
            (charts / "cassandra-0.15.3.tgz").unlink()
            (charts / "kafka-26.6.2.tgz").unlink()
            self.assertIn("set differs", check()[1])
            self.assertEqual(1, chart_archives.main(["dependencies", str(Path(temp) / "missing")]))


if __name__ == "__main__":
    unittest.main()
