# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import deploy
import helmfile_deploy as hd
import release_phases

ROOT = Path(__file__).resolve().parents[2]
HEAD = "c" * 40
CORE = "d" * 40
DIGEST = "sha256:" + "e" * 64
PLATFORM = "sha256:" + "f" * 64
COMPANIONS = [f"{ns}/demo-resources" for ns in release_phases.NAMESPACES]
APPLICATIONS = ["default/prometheus", "default/jaeger", "opensearch/opensearch", "opensearch/opensearch-dashboards",
                "jaeger/jaeger", "otel-demo/otel-demo"]
ORDER = ["default/prometheus", "default/jaeger", "default/demo-resources", "opensearch/opensearch",
         "opensearch/opensearch-dashboards", "jaeger/jaeger", "otel-demo/otel-demo", "jaeger/demo-resources",
         "opensearch/demo-resources", "otel-demo/demo-resources"]
# Words that would mean a value, a Secret or a diff reached the record.
FORBIDDEN_KEYS = {"values", "data", "secret", "secrets", "password", "diff", "manifest", "objects", "stringData"}


def done(args, stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class Cluster:
    """A fake runner holding Helm releases. helmfile apply changes only the releases in `changes`."""

    def __init__(self, statuses=None, changes=(), state=True):
        self.releases = {key: {"revision": 4, "status": "deployed", "chart": "c-1"} for key in APPLICATIONS + COMPANIONS}
        for key, status in (statuses or {}).items():
            if status is None:
                del self.releases[key]
            else:
                self.releases[key]["status"] = status
        self.changes, self.state, self.calls = set(changes), state, []

    def __call__(self, args, check=True, capture=False, env=None, cwd=None):
        args = [str(a) for a in args]
        self.calls.append(args)
        if args[:2] == ["git", "-C"]:
            return done(args, HEAD + "\n")
        if args[:2] == ["helm", "list"]:
            ns = args[3]
            listed = [{"name": key.split("/")[1], "namespace": ns, "status": rel["status"]}
                      for key, rel in self.releases.items() if key.startswith(ns + "/") and rel["status"] != "unknown"]
            return done(args, json.dumps(listed))
        if args[:2] == ["helm", "history"]:
            key = args[4] + "/" + args[2]
            if key not in self.releases:
                return done(args, "", 1, "Error: release: not found")
            rel = self.releases[key]
            return done(args, json.dumps([{"revision": rel["revision"], "status": rel["status"], "chart": rel["chart"],
                                           "app_version": "1"}]))
        if args[:2] == ["helm", "install"]:
            self.releases[args[args.index("--namespace") + 1] + "/demo-resources"] = {
                "revision": 1, "status": "deployed", "chart": "demo-resources-0.1.0"}
            return done(args)
        if args[0] == "helmfile":
            selector = dict(part.split("=") for part in args[args.index("-l") + 1].split(","))
            key = selector["namespace"] + "/" + selector["name"]
            method = args[args.index("--skip-deps") + 1]
            rel = self.releases[key]
            if method == "sync" or key in self.changes or rel["status"] != "deployed":
                rel.update(revision=rel["revision"] + 1, status="deployed")
            return done(args)
        if args[:5] == ["kubectl", "-n", "default", "get", "configmap"]:
            return done(args, json.dumps({"data": {}}) if self.state else "")
        return done(args)

    def writes(self):
        return [c for c in self.calls if c[0] == "helmfile" or c[:2] == ["helm", "install"]
                or (c[0] == "kubectl" and ("patch" in c or "restart" in c))]

    def cluster_calls(self):
        return [c for c in self.calls if c[0] in ("kubectl", "helm") or (c[0] == "helmfile" and "template" not in c)]


class Checks:
    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []

    def prepare(self, stacks):
        self.calls.append(("prepare", tuple(sorted(stacks))))

    def verify(self, stack):
        self.calls.append(("verify", stack))
        if stack in self.fail:
            raise RuntimeError(f"{stack} health failed")
        return {"health": {"stack": stack}}


def live_observed():
    images = {}
    for stack in ("oci", "otel"):
        images[stack] = {name: {"reference": f"docker.io/{repo}:{'1' * 40}", "sourceCommit": "1" * 40, "digest": DIGEST}
                         for name, repo in (("jaeger", "jaegertracing/jaeger-snapshot"),
                                            ("hotrod", "jaegertracing/example-hotrod-snapshot"))}
    policies = {stack: {"jaeger": "Always", "hotrod": "Always"} for stack in ("oci", "otel")}
    return images, policies


class DriverTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        for name in ("examples", "scripts", "charts", "helmfile.yaml", "helmfile-resources.yaml.gotmpl"):
            (self.root / name).symlink_to(ROOT / name)
        (self.root / "helmfile").mkdir()
        self.mode("live")
        self.summary = self.root / "summary.md"
        self.env = {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": HEAD,
                    "EXPECTED_CONFIG_SHA": HEAD, "DEPLOY_ENGINE": "helmfile", "HELMFILE_OPERATION": "apply",
                    "COMPANION_NAMESPACE": "none", "GITHUB_STEP_SUMMARY": str(self.summary), "MAIN_SHA": CORE}

    def tearDown(self):
        self.temp.cleanup()

    def mode(self, mode):
        (self.root / "helmfile/images.yaml").write_text(f"mode: {mode}\n")

    def reconcile(self, cluster, checks=None, **env):
        self.checks = checks or Checks()
        return hd.reconcile(dict(self.env, **env), root=self.root, run=cluster, checks=self.checks,
                            live_images=lambda run: live_observed(), verify_charts=lambda root: {"charts": {}})

    def record(self):
        return json.loads((self.root / hd.RECORD).read_text())

    def digest_inputs(self, receipt_sha=None):
        work = hd.workdir(self.root)
        sources = {name: {"repository": repo, "tag": "1" * 40, "sourceCommit": "1" * 40, "digest": DIGEST,
                          "platformManifest": PLATFORM, "reference": f"docker.io/{repo}:{'1' * 40}@{DIGEST}"}
                   for name, repo in (("jaeger", "jaegertracing/jaeger-snapshot"),
                                      ("hotrod", "jaegertracing/example-hotrod-snapshot"))}
        selection = {"mode": "promotion", "configCommit": HEAD, "coreMain": CORE, "platform": "linux/amd64",
                     "sources": sources, "images": {}}
        data = json.dumps(selection).encode()
        (work / hd.SELECTION).write_bytes(data)
        receipt = {"configCommit": HEAD, "selectionSHA256": receipt_sha or hashlib.sha256(data).hexdigest(),
                   "checks": {"oci": {"result": "passed"}, "otel": {"result": "passed"}}}
        (work / hd.RECEIPT).write_text(json.dumps(receipt))

    # ------------------------------------------------------------ commands

    def test_each_release_runs_alone_with_the_direct_flags_and_no_global_rollback(self):
        application = hd.helmfile_command(self.root, {"id": "default/jaeger", "kind": "application"}, "apply",
                                          "/v/images.json", "/v/companions.json")
        self.assertEqual(["helmfile", "-f", self.root / ".helmfile-locked.yaml", "--state-values-file", "/v/images.json",
                          "-l", "namespace=default,name=jaeger", "--skip-deps", "apply", "--concurrency", "1",
                          "--reset-values", "--sync-args=--install=false --create-namespace=false",
                          "--diff-args=--allow-unreleased=false", "--suppress-secrets", "--suppress-diff"], application)
        companion = hd.helmfile_command(self.root, {"id": "jaeger/demo-resources", "kind": "companion"}, "sync",
                                        "/v/images.json", "/v/companions.json")
        self.assertEqual(self.root / "helmfile-resources.yaml.gotmpl", companion[2])
        self.assertEqual("/v/companions.json", companion[4])
        self.assertIn("sync", companion)
        for command in (application, companion):
            text = " ".join(map(str, command))
            self.assertNotIn("rollback-on-failure", text)
            self.assertNotIn("--atomic", text)
            self.assertNotIn("take-ownership", text)
            self.assertNotIn("helmfile.yaml ", text)

    def test_all_ten_releases_run_oci_first_with_companions_after_their_stack(self):
        self.assertEqual(ORDER, [row["id"] for row in hd.release_rows(ROOT)])
        cluster = Cluster()
        self.mode("digest")
        self.digest_inputs()
        self.reconcile(cluster, GITHUB_EVENT_NAME="schedule", EXPECTED_CONFIG_SHA="")
        ran = [c[c.index("-l") + 1] for c in cluster.calls if c[0] == "helmfile"]
        self.assertEqual([f"namespace={k.split('/')[0]},name={k.split('/')[1]}" for k in ORDER], ran)
        self.assertEqual({str(self.root / hd.WORK / hd.SELECTION)}, {c[4] for c in cluster.calls
                                                                if c[0] == "helmfile" and "demo-resources" not in c[6]})

    # ------------------------------------------------------------ guards

    def assert_refused(self, message, cluster=None, **env):
        cluster = cluster or Cluster()
        with self.assertRaisesRegex(ValueError, message):
            self.reconcile(cluster, **env)
        self.assertEqual([], cluster.cluster_calls())
        self.assertEqual("refused", self.record()["outcome"])
        return cluster

    def test_guards_refuse_before_any_cluster_call(self):
        cases = [
            ("schedule or a manual dispatch", {"GITHUB_EVENT_NAME": "push"}),
            ("main only", {"GITHUB_REF": "refs/heads/topic"}),
            ("does not select", {"DEPLOY_ENGINE": "scripts"}),
            ("differs from the workflow revision", {"GITHUB_SHA": "a" * 40}),
            ("requires expected_config_sha", {"EXPECTED_CONFIG_SHA": ""}),
            ("requires expected_config_sha", {"EXPECTED_CONFIG_SHA": "a" * 40}),
            ("Unknown Helmfile operation", {"HELMFILE_OPERATION": "sync"}),
            ("only to install-companion", {"COMPANION_NAMESPACE": "default"}),
            ("needs one companion_namespace", {"HELMFILE_OPERATION": "install-companion"}),
            ("needs one companion_namespace", {"HELMFILE_OPERATION": "install-companion", "COMPANION_NAMESPACE": "kube-system"}),
            ("Only a manual dispatch installs", {"HELMFILE_OPERATION": "install-companion", "COMPANION_NAMESPACE": "default",
                                                 "GITHUB_EVENT_NAME": "schedule"}),
            ("scheduled Helmfile run resolves its images", {"GITHUB_EVENT_NAME": "schedule"}),
            ("remove the image tag inputs", {"JAEGER_DEMO_JAEGER_IMAGE_TAG": "abc"}),
        ]
        for message, env in cases:
            with self.subTest(message=message, env=env):
                self.assert_refused(message, **env)

    def test_a_bad_image_mode_is_refused(self):
        (self.root / "helmfile/images.yaml").write_text("mode: tags\n")
        self.assert_refused("must set mode")

    def test_a_missing_state_configmap_refuses_before_any_write(self):
        cluster = Cluster(state=False)
        with self.assertRaisesRegex(ValueError, "existing jaeger-demo-deployment-revisions"):
            self.reconcile(cluster)
        self.assertEqual([], cluster.writes())

    # ------------------------------------------------------------ images

    def test_failed_validation_leaves_no_receipt_and_reconcile_makes_no_cluster_call(self):
        self.mode("digest")
        renders = []

        def template(args, **kwargs):
            renders.append((args, kwargs.get("env", {}).get("KUBECONFIG")))
            return done(args, "kind: Deployment\n")

        def incompatible(*args):
            raise ValueError("Jaeger rejected the oci configuration")

        selection = {"sources": {}, "configCommit": HEAD}
        with self.assertRaisesRegex(ValueError, "rejected"):
            hd.images(dict(self.env), root=self.root, run=template, resolve=lambda event, overrides: selection,
                      validate=incompatible)
        self.assertEqual(2, len(renders))
        for args, kubeconfig in renders:
            self.assertEqual(hd.os.devnull, kubeconfig)
            self.assertIn("template", args)
            self.assertEqual("helmfile", args[0])
        self.assertFalse((self.root / hd.WORK / hd.RECEIPT).exists())
        cluster = self.assert_refused("No validated image selection")
        self.assertEqual([["git", "-C", str(self.root), "rev-parse", "HEAD"]], cluster.calls)

    def test_images_passes_overrides_and_renders_both_jaeger_releases_offline(self):
        self.mode("digest")
        seen = {}

        def resolve(event, overrides):
            seen.update(event=event, overrides=overrides)
            return {"sources": {"jaeger": {"reference": "r", "sourceCommit": "s"}}}

        def validate(data, digest, renders, runtime):
            seen.update(sha=digest == hashlib.sha256(data).hexdigest(), stacks=sorted(renders), runtime=runtime)
            return {"selectionSHA256": digest}

        calls = []
        hd.images(dict(self.env, JAEGER_DEMO_HOTROD_IMAGE_TAG="abc"), root=self.root,
                  run=lambda args, **kw: calls.append(args) or done(args, "x"), resolve=resolve, validate=validate)
        self.assertEqual({"event": "workflow_dispatch", "overrides": {"jaeger": None, "hotrod": "abc"}, "sha": True,
                          "stacks": ["oci", "otel"], "runtime": "docker"}, seen)
        self.assertEqual(["stack=oci,component=jaeger", "stack=otel,component=jaeger"],
                         [c[c.index("-l") + 1] for c in calls])
        self.assertTrue((self.root / hd.WORK / hd.RECEIPT).is_file())

    def test_images_in_live_mode_does_nothing_but_refuses_a_schedule(self):
        hd.images(dict(self.env), root=self.root, run=None, resolve=None, validate=None)
        with self.assertRaisesRegex(ValueError, "resolves its images"):
            hd.images(dict(self.env, GITHUB_EVENT_NAME="schedule"), root=self.root)

    def test_a_receipt_for_another_selection_is_refused(self):
        self.mode("digest")
        self.digest_inputs(receipt_sha="0" * 64)
        self.assert_refused("receipt is for another selection")

    # ------------------------------------------------------------ live mode

    def test_live_mode_reuses_tag_references_without_digest_syntax(self):
        observed, policies = live_observed()
        values, record = hd.live_image_values(observed, policies)
        for stack in ("oci", "otel"):
            for name in ("jaeger", "hotrod"):
                image = values["images"][stack][name]
                self.assertNotIn("@", image["tag"])
                self.assertEqual(observed[stack][name]["reference"], f"docker.io/{image['repository']}:{image['tag']}")
                self.assertEqual(DIGEST, record[stack][name]["digest"])
        cluster = Cluster()
        self.reconcile(cluster)
        written = (self.root / hd.WORK / hd.LIVE_VALUES).read_text()
        self.assertNotIn("sha256", written)
        self.assertEqual(hd.NOT_REVALIDATED, self.record()["compatibility"])
        self.assertEqual(CORE, self.record()["coreMain"])
        for ref in (f"docker.io/jaegertracing/jaeger-snapshot:{'1' * 40}@{DIGEST}",
                    f"jaegertracing/jaeger-snapshot:{'1' * 40}"):
            observed["oci"]["jaeger"]["reference"] = ref
            with self.subTest(ref=ref), self.assertRaisesRegex(ValueError, "not a tag in the form"):
                hd.live_image_values(observed, policies)

    # ------------------------------------------------------------ phases

    def test_an_empty_diff_still_verifies_both_stacks_and_creates_no_revision(self):
        cluster = Cluster()
        record = self.reconcile(cluster)
        self.assertEqual([("prepare", ("oci", "otel")), ("verify", "oci"), ("verify", "otel")], self.checks.calls)
        self.assertEqual(["oci", "otel"], record["verifiedStacks"])
        self.assertTrue(all(r["revisionBefore"] == r["revisionAfter"] == 4 and not r["changed"] and r["method"] == "apply"
                            for r in record["releases"]))
        self.assertEqual("succeeded", self.record()["outcome"])

    def test_a_failed_release_with_an_empty_diff_is_synced(self):
        cluster = Cluster({"default/prometheus": "failed"})
        record = self.reconcile(cluster)
        prometheus, = [c for c in cluster.calls if c[0] == "helmfile" and "namespace=default,name=prometheus" in c]
        self.assertEqual("sync", prometheus[prometheus.index("--skip-deps") + 1])
        entry = record["releases"][0]
        self.assertEqual(("default/prometheus", "failed", 4, "sync", 5, True),
                         (entry["id"], entry["statusBefore"], entry["revisionBefore"], entry["method"],
                          entry["revisionAfter"], entry["changed"]))

    def test_a_pending_or_unknown_release_refuses_before_any_write(self):
        for status in ("pending-upgrade", "pending-rollback", "pending-install", "unknown", None):
            with self.subTest(status=status):
                cluster = Cluster({"otel-demo/otel-demo": status})
                with self.assertRaisesRegex(ValueError, "operator review"):
                    self.reconcile(cluster)
                self.assertEqual([], cluster.writes())
                self.assertEqual(["prepare"], [c[0] for c in self.checks.calls])
                self.assertEqual("refused", self.record()["outcome"])

    def test_failed_oci_verification_stops_before_any_otel_call(self):
        cluster = Cluster(changes={"default/jaeger", "jaeger/jaeger"})
        with self.assertRaisesRegex(RuntimeError, "oci health failed"):
            self.reconcile(cluster, Checks(fail={"oci"}))
        otel = [c for c in cluster.calls if c[0] == "helmfile" and not c[c.index("-l") + 1].startswith("namespace=default,")]
        self.assertEqual([], otel)
        self.assertEqual([], [c for c in cluster.writes() if c[0] == "kubectl"])
        self.assertEqual([("prepare", ("oci", "otel")), ("verify", "oci")], self.checks.calls)
        record = self.record()
        self.assertEqual("failed", record["outcome"])
        self.assertEqual([], record["verifiedStacks"])
        self.assertEqual([(5, "apply"), (None, None)], [(r["revisionAfter"], r["method"]) for r in record["releases"]
                                                        if r["id"] in ("default/jaeger", "jaeger/jaeger")])

    def test_fingerprints_are_patched_one_key_at_a_time_per_verified_stack(self):
        cluster = Cluster()
        with self.assertRaises(RuntimeError):
            self.reconcile(cluster, Checks(fail={"otel"}))
        patches = [json.loads(c[-1]) for c in cluster.calls if c[:4] == ["kubectl", "-n", "default", "patch"]]
        self.assertEqual([{"data": {name: deploy.fingerprint(name, self.root)}} for name in ("oci-jaeger", "oci-monitoring")],
                         patches)
        self.assertEqual(["oci-jaeger", "oci-monitoring"], self.record()["fingerprints"])
        cluster = Cluster()
        self.reconcile(cluster)
        patched = [list(json.loads(c[-1])["data"]) for c in cluster.calls if c[:4] == ["kubectl", "-n", "default", "patch"]]
        self.assertEqual([[name] for name in ("oci-jaeger", "oci-monitoring", "opensearch", "otel-jaeger", "otel-demo")],
                         patched)

    def test_the_generator_restarts_after_each_verified_stack(self):
        cluster = Cluster()
        self.reconcile(cluster)
        restarts = [c[2] for c in cluster.calls if "restart" in c]
        self.assertEqual(["default", "jaeger"], restarts)
        # The OCI restart follows the OCI checks and precedes the first OTel release.
        index = [i for i, c in enumerate(cluster.calls) if "restart" in c][0]
        first_otel = [i for i, c in enumerate(cluster.calls) if c[0] == "helmfile" and "namespace=opensearch,name=opensearch" in c][0]
        self.assertLess(index, first_otel)

    def test_install_companion_installs_only_an_absent_named_companion_without_atomic_or_ownership(self):
        env = {"HELMFILE_OPERATION": "install-companion", "COMPANION_NAMESPACE": "jaeger"}
        cluster = Cluster({"jaeger/demo-resources": None})
        record = self.reconcile(cluster, **env)
        install, = [c for c in cluster.calls if c[:2] == ["helm", "install"]]
        self.assertEqual(["helm", *release_phases.install_args("jaeger", self.root / "charts/demo-resources",
                                                               self.root / hd.WORK / "companion-jaeger.json")], install)
        self.assertNotIn("--take-ownership", install)
        self.assertIn("--rollback-on-failure=false", install)
        self.assertEqual([], [c for c in cluster.calls if c[0] == "helmfile"])
        self.assertEqual(["jaeger/demo-resources"], [r["id"] for r in record["releases"]])
        self.assertEqual(("absent", None, 1, True), (record["releases"][0]["statusBefore"], record["releases"][0]["revisionBefore"],
                                                     record["releases"][0]["revisionAfter"], record["releases"][0]["changed"]))
        self.assertEqual(["oci", "otel"], record["verifiedStacks"])
        self.assertEqual([], record["fingerprints"])
        values = json.loads((self.root / hd.WORK / "companion-jaeger.json").read_text())
        self.assertEqual({"trace-script", "trace-generator", "jaeger-demo-ingress", "jaeger-query-clusterip"},
                         {o["metadata"]["name"] for o in values["objects"]})
        # A present companion is never reinstalled.
        cluster = Cluster()
        with self.assertRaisesRegex(ValueError, "not absent"):
            self.reconcile(cluster, **env)
        self.assertEqual([], cluster.writes())

    def test_an_absent_companion_blocks_apply(self):
        cluster = Cluster({"opensearch/demo-resources": None})
        with self.assertRaisesRegex(ValueError, "explicit first install"):
            self.reconcile(cluster)
        self.assertEqual([], cluster.writes())

    # ------------------------------------------------------------ record

    def test_the_record_holds_provenance_but_no_value_secret_or_diff(self):
        self.mode("digest")
        self.digest_inputs()
        cluster = Cluster(changes={"default/jaeger"})
        self.reconcile(cluster, GITHUB_EVENT_NAME="schedule", EXPECTED_CONFIG_SHA="")
        record = self.record()

        def keys(node):
            if isinstance(node, dict):
                for key, value in node.items():
                    yield key
                    yield from keys(value)
            elif isinstance(node, list):
                for value in node:
                    yield from keys(value)

        self.assertEqual(set(), FORBIDDEN_KEYS & set(keys(record)))
        text = (self.root / hd.RECORD).read_text()
        generator = (ROOT / "examples/oci/load-generator/generate_traces.py").read_text()
        self.assertNotIn(generator[:200], text)
        self.assertEqual((HEAD, CORE, "digest", "succeeded"),
                         (record["configCommit"], record["coreMain"], record["imageMode"], record["outcome"]))
        self.assertEqual({"oci", "otel"}, set(record["images"]))
        self.assertEqual({"reference", "sourceCommit", "digest"}, set(record["images"]["oci"]["jaeger"]))
        self.assertEqual({"helm", "helmfile", "helm-diff", "kubeconform"}, set(record["tools"]))
        self.assertEqual("v4.3.2", record["tools"]["helm"]["version"])
        self.assertEqual(["default/jaeger"], [r["id"] for r in record["releases"] if r["changed"]])
        self.assertIn("passed for selection", self.summary.read_text())
        self.assertIn("| default/jaeger | deployed | 4 | apply | 5 | yes |", self.summary.read_text())

    # ------------------------------------------------------------ locked charts

    def test_locked_charts_must_be_current_and_match_the_lock(self):
        root = self.root
        (root / "scripts").unlink()
        (root / "scripts/demo").mkdir(parents=True)
        lock = {"charts": {"a-1.0.0.tgz": hashlib.sha256(b"a").hexdigest()}, "jaegerDependencies": {}}
        (root / "scripts/demo/chart-archives.lock.json").write_text(json.dumps(lock))
        (root / ".tools/chart-archives").mkdir(parents=True)
        (root / ".tools/chart-archives/a-1.0.0.tgz").write_bytes(b"a")
        (root / ".tools/helm-charts/charts/jaeger/charts").mkdir(parents=True)

        def state(source_sha=None):
            source = source_sha or hashlib.sha256((root / "helmfile.yaml").read_bytes()).hexdigest()
            locked = hashlib.sha256((root / "scripts/demo/chart-archives.lock.json").read_bytes()).hexdigest()
            (root / ".helmfile-locked.yaml").write_text(
                f"# Generated\n# helmfile.yaml sha256: {source}\n# scripts/demo/chart-archives.lock.json sha256: {locked}\n"
                "releases:\n  - {name: a, namespace: default, chart: ./.tools/chart-archives/a-1.0.0.tgz}\n"
                "  - {name: jaeger, namespace: default, chart: ./.tools/helm-charts/charts/jaeger}\n")

        state()
        self.assertEqual({"a-1.0.0.tgz": lock["charts"]["a-1.0.0.tgz"]}, hd.verify_locked_charts(root)["charts"])
        state("0" * 64)
        with self.assertRaisesRegex(ValueError, "stale"):
            hd.verify_locked_charts(root)
        state()
        (root / ".tools/chart-archives/a-1.0.0.tgz").write_bytes(b"b")
        with self.assertRaisesRegex(ValueError, "differs from its reviewed SHA-256"):
            hd.verify_locked_charts(root)
        (root / ".helmfile-locked.yaml").unlink()
        with self.assertRaisesRegex(ValueError, "run helmfile_deploy.py prepare"):
            hd.verify_locked_charts(root)

    # ------------------------------------------------------------ production checks

    def test_production_checks_compare_the_data_binding_and_require_ready_certificates(self):
        calls = []
        bindings = iter([("claim", "uid", "pv"), ("claim", "uid", "pv")])
        certificates = {"items": [{"metadata": {"name": "c"}, "status": {"conditions": [{"type": "Ready", "status": "True"}]}}]}

        def run(args, **kwargs):
            calls.append([str(a) for a in args])
            return done(args, json.dumps(certificates))

        checks = hd.ProductionChecks(ROOT, run, health=lambda stack: {"stack": stack},
                                     trace=lambda *versions: calls.append(["trace", *versions]),
                                     binding=lambda: next(bindings), sleep=lambda s: None)
        checks.prepare({"oci", "otel"})
        evidence = checks.verify("otel")
        self.assertEqual(3, evidence["readyCertificates"])
        self.assertIn(["trace", "3.10.0", "3.10.0"], calls)
        self.assertEqual(len(hd.OTEL_ROLLOUTS), len([c for c in calls if "rollout" in c]))
        self.assertTrue(evidence["openSearchDataBindingUnchanged"])
        bindings = iter([("claim", "uid", "pv"), ("claim", "uid", "other-pv")])
        checks = hd.ProductionChecks(ROOT, run, health=lambda stack: {}, trace=lambda *v: None,
                                     binding=lambda: next(bindings), sleep=lambda s: None)
        checks.prepare({"otel"})
        with self.assertRaisesRegex(RuntimeError, "data claim or its bound volume changed"):
            checks.verify("otel")
        certificates["items"][0]["status"]["conditions"][0]["status"] = "False"
        checks = hd.ProductionChecks(ROOT, run, health=lambda stack: {}, trace=lambda *v: None,
                                     binding=lambda: ("c", "u", "p"), sleep=lambda s: None)
        checks.prepare({"otel"})
        with self.assertRaisesRegex(RuntimeError, "is not Ready"):
            checks.verify("otel")
        oci = hd.ProductionChecks(ROOT, run, health=lambda stack: {"stack": stack}, sleep=lambda s: None)
        calls.clear()
        self.assertEqual({"stack": "oci"}, oci.verify("oci")["health"])
        self.assertEqual([["kubectl", "-n", "default", "rollout", "status", f"deployment/{name}", "--timeout=5m"]
                          for name in ("jaeger", "jaeger-hotrod", "trace-generator", "prometheus-grafana")], calls)

    def test_health_is_retried_until_it_passes_or_the_attempts_run_out(self):
        attempts = []

        def flaky():
            attempts.append(1)
            if len(attempts) < 3:
                raise ValueError("No recent matching spans")
            return "ok"

        self.assertEqual("ok", hd.retry(flaky, attempts=5, sleep=lambda s: None))
        attempts.clear()
        with self.assertRaisesRegex(ValueError, "No recent"):
            hd.retry(flaky, attempts=2, sleep=lambda s: None)


if __name__ == "__main__":
    unittest.main()
