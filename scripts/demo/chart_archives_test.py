# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import contextlib
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

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


class LockedStateTests(unittest.TestCase):
    """locked_state() on a copy of the real Helmfile and pins, with a fake Helm pull."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        for path in ("helmfile.yaml", "examples/oci/deploy-all.sh", "examples/otel-demo/deploy-all.sh"):
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_bytes((chart_archives.ROOT / path).read_bytes())
        (self.root / "scripts/demo").mkdir(parents=True)
        self.dest = self.root / ".tools/chart-archives"
        self.dest.mkdir(parents=True)
        self.tampered = set()
        self.calls = []
        self.versions = {name: chart_archives.pin(self.root, variable) for _, variable, name, _ in chart_archives.CHARTS}
        names = [f"{name}-{version}.tgz" for name, version in self.versions.items()]
        self.lock = {"charts": {n: sha(b"archive " + n.encode()) for n in names}, "jaegerDependencies": {}}
        self.write_lock()

    def write_lock(self):
        (self.root / chart_archives.LOCK).write_text(json.dumps(self.lock))

    def run_helm(self, args, **kwargs):
        self.calls.append(args)
        chart, version = args[2], args[args.index("--version") + 1]
        name = f"{chart}-{version}.tgz"
        body = b"tampered" if chart in self.tampered else b"archive " + name.encode()
        (Path(args[args.index("--destination") + 1]) / name).write_bytes(body)
        return subprocess.CompletedProcess(args, 0)

    def state(self):
        return chart_archives.locked_state(self.root, self.dest, run=self.run_helm)

    def replace(self, relative, old, new):
        path = self.root / relative
        text = path.read_text()
        self.assertEqual(1, text.count(old), f"{old!r} must occur exactly once in {relative}")
        path.write_text(text.replace(old, new))

    def test_only_chart_fields_and_repositories_differ_from_helmfile(self):
        original = yaml.safe_load((self.root / "helmfile.yaml").read_text())
        derived = yaml.safe_load(self.state().read_text())
        self.assertNotIn("repositories", derived)
        self.assertEqual({k: v for k, v in original.items() if k not in ("repositories", "releases")},
                         {k: v for k, v in derived.items() if k != "releases"})
        self.assertEqual(len(original["releases"]), len(derived["releases"]))
        locked = 0
        for before, after in zip(original["releases"], derived["releases"]):
            self.assertEqual({k: v for k, v in before.items() if k != "chart"},
                             {k: v for k, v in after.items() if k != "chart"})
            if before["chart"].startswith("./"):
                self.assertEqual(before["chart"], after["chart"])
                continue
            locked += 1
            name = before["chart"].split("/")[1]
            self.assertEqual(f"./.tools/chart-archives/{name}-{before['version']}.tgz", after["chart"])
            self.assertEqual(b"archive " + (self.root / after["chart"]).name.encode(), (self.root / after["chart"]).read_bytes())
        self.assertEqual(4, locked)

    def test_text_is_helmfile_with_only_chart_lines_and_repositories_changed(self):
        before = (self.root / "helmfile.yaml").read_text().splitlines()
        after = self.state().read_text().splitlines()
        self.assertEqual(3, len([line for line in after if line.startswith("# ") and "sha256" in line or line.startswith("# Generated")]))
        body = [line for line in after if not line.startswith("# Generated") and "sha256:" not in line]
        removed = [line for line in before if line not in body]
        self.assertTrue(all(line.startswith(("repositories:", "  - name:", "    url:")) or "chart: " in line for line in removed))
        self.assertEqual(4, len([line for line in body if "chart: ./.tools/chart-archives/" in line]))

    def test_header_records_the_inputs_and_output_is_deterministic(self):
        first = self.state()
        text = first.read_text()
        self.assertIn("# helmfile.yaml sha256: " + sha((self.root / "helmfile.yaml").read_bytes()), text)
        self.assertIn(f"# {chart_archives.LOCK} sha256: " + sha((self.root / chart_archives.LOCK).read_bytes()), text)
        self.assertEqual(text, self.state().read_text())
        self.assertEqual(sorted(f"{n}-{v}.tgz" for n, v in self.versions.items()), sorted(p.name for p in self.dest.iterdir()))

    def test_local_jaeger_chart_path_is_unchanged(self):
        derived = yaml.safe_load(self.state().read_text())
        jaegers = [r["chart"] for r in derived["releases"] if r["name"] == "jaeger"]
        self.assertEqual(["./.tools/helm-charts/charts/jaeger"] * 2, jaegers)

    def test_version_mismatch_is_refused_before_any_pull_and_names_the_wrong_side(self):
        dashboards = self.versions["opensearch-dashboards"]
        cases = (
            ("helmfile", lambda: self.replace("helmfile.yaml", f'chart: opensearch/opensearch-dashboards\n    version: "{dashboards}"',
                                              'chart: opensearch/opensearch-dashboards\n    version: "0.0.1"'),
             "differs from the deploy-all.sh pin"),
            ("pin", lambda: self.replace("examples/oci/deploy-all.sh", self.versions["kube-prometheus-stack"], "0.0.1"),
             "differs from the deploy-all.sh pin"),
            ("lock", lambda: self.drop_lock(f"opensearch-dashboards-{dashboards}.tgz"), "has no entry in"),
        )
        for label, mutate, message in cases:
            with self.subTest(label):
                self.setUp()
                mutate()
                with self.assertRaisesRegex(ValueError, message):
                    self.state()
                self.assertEqual([], self.calls)

    def drop_lock(self, name):
        del self.lock["charts"][name]
        self.write_lock()

    def test_unlocked_chart_repository_and_top_level_keys_are_refused(self):
        self.replace("helmfile.yaml", "chart: opensearch/opensearch\n", "chart: opensearch/other\n")
        with self.assertRaisesRegex(ValueError, "unlocked chart"):
            self.state()
        self.setUp()
        self.replace("helmfile.yaml", "url: https://opensearch-project.github.io/helm-charts", "url: https://example.com/charts")
        with self.assertRaisesRegex(ValueError, "not the locked publisher"):
            self.state()
        self.setUp()
        with (self.root / "helmfile.yaml").open("a") as handle:
            handle.write("environments: {}\n")
        with self.assertRaisesRegex(ValueError, "top-level keys"):
            self.state()
        self.assertEqual([], self.calls)

    def test_an_archive_with_another_hash_is_refused_deleted_and_no_state_written(self):
        self.tampered.add("opensearch-dashboards")
        with self.assertRaisesRegex(ValueError, "differs from approved SHA-256: opensearch-dashboards-"):
            self.state()
        self.assertEqual([], [p for p in self.dest.rglob("opensearch-dashboards-*.tgz")])
        self.assertFalse((self.root / chart_archives.LOCKED_STATE).exists())

    def test_a_refused_run_removes_the_previous_state(self):
        self.state()
        self.assertTrue((self.root / chart_archives.LOCKED_STATE).is_file())
        self.tampered.add("opensearch")
        with self.assertRaises(ValueError):
            self.state()
        self.assertFalse((self.root / chart_archives.LOCKED_STATE).exists())
        self.assertFalse((self.root / (chart_archives.LOCKED_STATE + ".tmp")).exists())

    def test_stale_pull_directories_are_removed_and_archives_are_always_re_pulled(self):
        stale = self.dest / "opensearch-abcd1234"
        stale.mkdir()
        (stale / "old.tgz").write_bytes(b"x")
        self.state()
        self.assertFalse(stale.exists())
        self.assertEqual(4, len(self.calls))
        self.state()
        self.assertEqual(8, len(self.calls))

    def test_state_subcommand_writes_the_derived_state_and_reports_bad_input_in_one_line(self):
        stdout = io.StringIO()
        with mock.patch.object(chart_archives, "ROOT", self.root), \
                mock.patch.object(chart_archives.subprocess, "run", self.run_helm), contextlib.redirect_stdout(stdout):
            self.assertEqual(0, chart_archives.main(["state", str(self.dest)]))
        self.assertEqual(str((self.root / chart_archives.LOCKED_STATE).resolve()), stdout.getvalue().strip())
        (self.root / "helmfile.yaml").write_text("releases: [unclosed\n")
        stderr = io.StringIO()
        with mock.patch.object(chart_archives, "ROOT", self.root), contextlib.redirect_stderr(stderr):
            self.assertEqual(1, chart_archives.main(["state", str(self.dest)]))
        self.assertEqual(1, len(stderr.getvalue().strip().splitlines()))
        self.assertTrue(stderr.getvalue().startswith("error: "))
        (self.root / "helmfile.yaml").write_text("releases: [{name: x}]\n")
        with mock.patch.object(chart_archives, "ROOT", self.root), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(1, chart_archives.main(["state", str(self.dest)]))

    def test_pull_and_dependencies_do_not_need_pyyaml(self):
        with mock.patch.dict(sys.modules, {"yaml": None}):
            fixture = Fixture(self.temp.name + "-x")
            self.addCleanup(shutil.rmtree, self.temp.name + "-x", True)
            self.assertEqual(b"reviewed chart", fixture.pull().read_bytes())
            with mock.patch.object(chart_archives, "ROOT", self.root), contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(1, chart_archives.main(["state", str(self.dest)]))
            self.assertIn("error: ", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
