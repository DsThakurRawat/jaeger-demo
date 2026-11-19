# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import copy
import io
import json
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock
import warnings
import zipfile

from cryptography.fernet import Fernet, InvalidToken

import adoption_bundle as b


def chart(files):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.SYMTYPE
                info.linkname = "/elsewhere"
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return result.getvalue()


class BundleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.checkout, self.source, self.dest = (self.root / k for k in ("repo", "input", "output"))
        for path in (self.checkout, self.source, self.dest):
            path.mkdir(mode=0o700)
        self.cipher = Fernet(Fernet.generate_key())
        self.key = "default/prometheus"
        self.obj = "v1:Secret:default:grafana"
        self.chart = chart({"kube-prometheus-stack/Chart.yaml": b"apiVersion: v2\nname: kube-prometheus-stack\nversion: 82.10.4\n"})
        self.record_sha = "b" * 64
        self.row = {"id": self.key, "needs": [], "checks": ["grafana-ready", "jaeger-scrape"],
                    "observe": [self.obj], "rollback": {"strategy": "helm-rollback", "revision": 7,
                    "recordSHA256": self.record_sha}, "chart": {"name": "kube-prometheus-stack",
                    "version": "82.10.4", "source": "prometheus-community/kube-prometheus-stack", "sha256": b.digest(self.chart)}}
        self.row["helm"] = {"operation":"upgrade", "wait":"legacy", "atomic":False, "skipCRDs":False,
                            "historyMax":10, "createNamespace":False, "timeoutSeconds":1200}
        self.plan = {"format": b.FORMAT, "configurationSHA": "placeholder", "mode": "adoption",
                     "requested": [self.key], "releases": [self.row], "images": {}}
        self.plan["selector"] = {"stack":"oci","component":"monitoring"}
        self.plan["renderContext"] = {"kubernetesVersion":"v1.36.1", "apiVersions":["v1"]}
        self.live = {"capabilitiesSHA256":b.digest(b.canonical(self.plan["renderContext"])), "cluster": "fixture-cluster", "healthy": True,
                     "objects": {self.obj: {"uid": "uid-1", "stateSHA256": "c" * 64}},
                     "releases": {self.key: {"status": "deployed", "revision": 7, "recordSHA256": self.record_sha}},
                     "rollbackTargets": {self.key: {"status": "deployed", "revision": 7, "recordSHA256": self.record_sha}}}
        self.lock = {"tools": {k: {"repository": k + "/" + k, "version": "v1.0.0",
                      "sha256": {"v1.0.0/linux-amd64": "f"*64}}
                      for k in ("helm", "helmfile", "helm-diff", "kubeconform")}, "kubernetesSchema": {"version":"v1.36.1","sha256": "c"*64}}
        lock = self.checkout / "scripts/demo/helmfile-tools.lock.json"
        lock.parent.mkdir(parents=True)
        lock.write_bytes(b.canonical(self.lock))
        state = {"releases": [
            {"namespace":"default","name":"prometheus","labels":{"stack":"oci","component":"monitoring"},
             "chart":"prometheus-community/kube-prometheus-stack","version":"82.10.4"},
            {"namespace":"otel-demo","name":"otel-demo","labels":{"stack":"otel","component":"otel-demo"},
             "chart":"open-telemetry/opentelemetry-demo","version":"0.40.9","needs":["jaeger/jaeger"]},
        ]}
        (self.checkout / "helmfile.yaml").write_bytes(b.canonical(state))
        for cmd in (["git", "init", "-q"], ["git", "add", "helmfile.yaml", "scripts/demo/helmfile-tools.lock.json"], ["git", "-c", "user.name=Fixture", "-c", "user.email=test@example.invalid",
                    "commit", "-q", "--allow-empty", "-m", "Fixture"]):
            subprocess.run(cmd, cwd=self.checkout, check=True)
        self.commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=self.checkout, text=True).strip()
        self.plan["configurationSHA"] = self.commit
        self.base = self.source / "releases/default/prometheus"
        self.base.mkdir(parents=True)
        (self.base / "values.yaml").write_text("adminPassword: fixture-private-value\n")
        (self.base / "rendered.yaml").write_text("apiVersion: v1\nkind: Secret\nmetadata: {name: grafana}\nstringData: {password: fixture-private-value}\n")
        (self.base / "chart.tgz").write_bytes(self.chart)

    def build(self):
        for name, doc in (("tools.lock.json", self.lock), ("plan.json", self.plan), ("live.json", self.live)):
            (self.source / name).write_bytes(b.canonical(doc))
        receipt = b.build(self.source, self.dest, self.cipher, self.commit, self.checkout)
        sha = receipt["bundleSHA256"]
        self.assertEqual({"bundleSHA256": sha, "configurationSHA": self.commit}, receipt)
        return self.dest / (sha + ".fernet"), sha

    def rewrite(self, path, change):
        raw = self.cipher.decrypt(path.read_bytes())
        with zipfile.ZipFile(io.BytesIO(raw)) as src:
            records = [(item, src.read(item)) for item in src.infolist()]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as out:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                for info, data in change(records):
                    out.writestr(info, data)
        new = self.cipher.encrypt(buf.getvalue())
        path.write_bytes(new)
        return b.digest(new)

    @staticmethod
    def render(plan, files):
        return {r["id"]: files[f"releases/{r['id']}/rendered.yaml"] for r in plan["releases"]}

    def test_approved_envelope_private_and_exact_inputs_reach_executor_after_live_read(self):
        path, sha = self.build()
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        self.assertNotIn(b"fixture-private-value", path.read_bytes())
        observer = Mock(return_value=self.live)
        def apply(row, method, files):
            self.assertEqual(2, observer.call_count)
            self.assertNotIn("stateSHA256", str(observer.call_args))
            self.assertEqual(self.row, row)
            self.assertEqual("apply", method)
            self.assertEqual({"chart.tgz", "values.yaml", "rendered.yaml"}, set(files))
            self.assertEqual(self.chart, files["chart.tgz"])
            self.assertIn(b"fixture-private-value", files["rendered.yaml"])
        health = Mock(return_value=True)
        receipt = b.consume(path, self.cipher, sha, self.checkout, self.commit, self.render, observer, apply, health)
        self.assertEqual({"releases": {self.key: "apply"}, "verifiedStacks": ["oci"]}, receipt)
        health.assert_called_once_with("oci")

    def test_drift_approval_key_source_and_read_failure_block_executor(self):
        path, sha = self.build()
        apply = Mock()
        with self.assertRaisesRegex(b.BundleError, "reproduce"):
            b.consume(path, self.cipher, sha, self.checkout, self.commit, lambda plan, files: {}, self.fail, apply, self.fail)
        changed = copy.deepcopy(self.live)
        changed["objects"][self.obj]["uid"] = "replacement"
        with self.assertRaisesRegex(b.BundleError, "drifted"):
            b.consume(path, self.cipher, sha, self.checkout, self.commit, self.render, lambda scope: changed, apply, self.fail)
        broken = Mock(side_effect=OSError("API denied"))
        with self.assertRaises(OSError):
            b.consume(path, self.cipher, sha, self.checkout, self.commit, self.render, broken, apply, self.fail)
        for cipher, expected in ((self.cipher, "a"*64), (Fernet(Fernet.generate_key()), sha)):
            with self.assertRaises((InvalidToken, b.BundleError)):
                b.consume(path, cipher, expected, self.checkout, self.commit, self.render, self.fail, apply, self.fail)
        with self.assertRaisesRegex(b.BundleError, "checkout"):
            b.consume(path, self.cipher, sha, self.checkout, "a"*40, self.render, self.fail, apply, self.fail)
        (self.checkout / "unexpected").write_text("dirty")
        with self.assertRaisesRegex(b.BundleError, "local changes"):
            b.consume(path, self.cipher, sha, self.checkout, self.commit, self.render, self.fail, apply, self.fail)
        apply.assert_not_called()

    def test_bundle_rejects_unexpected_duplicate_link_compressed_missing_or_modified_members(self):
        def other(rows):
            info = copy.copy(rows[0][0])
            info.filename = "../unexpected"
            return rows + [(info, b"")]
        def link(rows):
            rows[0][0].external_attr = stat.S_IFLNK << 16
            return rows
        def compress(rows):
            rows[0][0].compress_type = zipfile.ZIP_DEFLATED
            return rows
        for change in (other, link, compress, lambda r: r+r[:1], lambda r: r[:-1],
                       lambda r: [(i, v+b" " if i.filename == "tools.lock.json" else v) for i,v in r]):
            path, _ = self.build()
            sha = self.rewrite(path, change)
            with self.subTest(change=change), self.assertRaises((b.BundleError, KeyError)):
                b.unseal(path, self.cipher, sha, self.commit)

    def test_companion_package_cannot_add_a_hook_or_remove_keep(self):
        root = self.checkout / "charts/demo-resources"
        root.mkdir(parents=True)
        descriptor = b"name: demo-resources\nversion: 0.1.0\n"
        allowed = b"default: {Service: [fixture]}\n"
        for name, data in (("Chart.yaml",descriptor), ("allowlist.yaml",allowed)):
            (root / name).write_bytes(data)
        subprocess.run(["git", "add", "charts"], cwd=self.checkout, check=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=test@example.invalid",
                        "commit", "-qm", "Companion fixture"], cwd=self.checkout, check=True)
        commit = subprocess.check_output(["git","rev-parse","HEAD"],cwd=self.checkout,text=True).strip()
        key = "default/demo-resources"
        row = {"id":key,"chart":{"source":"charts/demo-resources","name":"demo-resources","version":"0.1.0"},"needs":[]}
        plan = {"mode":"adoption","selector":{"stack":"oci","component":"demo-resources"},
                "requested":[key],"releases":[row]}
        members = {"demo-resources/Chart.yaml":descriptor,"demo-resources/allowlist.yaml":allowed}
        render = b"apiVersion: v1\nkind: Service\nmetadata: {name: fixture, annotations: {helm.sh/resource-policy: keep}}"
        prefix = f"releases/{key}/"
        files = {"tools.lock.json":b.canonical(self.lock),prefix+"chart.tgz":chart(members),prefix+"rendered.yaml":render}
        b.verify_frozen_source(self.checkout,commit,files,plan)
        files[prefix+"rendered.yaml"] = render.replace(b"keep",b"remove")
        with self.assertRaisesRegex(b.BundleError, "Unsafe"):
            b.verify_frozen_source(self.checkout,commit,files,plan)
        files[prefix+"chart.tgz"] = chart(dict(members, **{"demo-resources/templates/extra.yaml":b"unexpected"}))
        with self.assertRaisesRegex(b.BundleError, "Unexpected companion chart files"):
            b.verify_frozen_source(self.checkout,commit,files,plan)

    def test_omitting_transitive_otel_prerequisites_cannot_change_source_selection(self):
        self.plan["requested"] = ["otel-demo/otel-demo"]
        self.plan["selector"] = {"stack":"otel", "component":"otel-demo"}
        self.row.update(id="otel-demo/otel-demo", needs=[])
        files = {"tools.lock.json": b.canonical(self.lock)}
        with self.assertRaisesRegex(b.BundleError, "checked-in Helmfile"):
            b.verify_frozen_source(self.checkout, self.commit, files, self.plan)

    def test_image_gate_binds_exact_selection_and_render(self):
        source = {"reference":"docker.io/jaegertracing/jaeger-snapshot:abc@sha256:"+"a"*64,
                  "sourceCommit":"b"*40, "digest":"sha256:"+"a"*64}
        images = {"oci":{"jaeger":source,"hotrod":dict(source, reference="docker.io/jaegertracing/example-hotrod:abc@sha256:"+"a"*64)}}
        candidate = {"mode":"promotion", "configCommit":self.commit,"coreMain":"a"*40,
                     "sources":{name:dict(src, platformManifest="sha256:"+"f"*64) for name,src in images["oci"].items()}}
        selection = b.canonical(candidate)
        render = b"frozen Jaeger release"
        proof = {"mode":"promotion","configCommit":self.commit,"coreMain":"a"*40,
                 "selectionSHA256":b.digest(selection),"images":images,"checks":{"oci":{"result":"passed",
                 "renderSHA256":b.digest(render),"configSHA256":"0"*64,"uiConfigSHA256":"1"*64,
                 "validatedPlatformManifest":"sha256:"+"f"*64}}}
        files = {"image-selection.json":selection,"image-compatibility.json":b.canonical(proof),
                 "releases/default/jaeger/rendered.yaml":render}
        b.check_compatibility(files, self.commit, images)
        with self.assertRaisesRegex(b.BundleError, "differs"):
            b.check_compatibility(dict(files, **{"releases/default/jaeger/rendered.yaml":b"other"}), self.commit, images)
        with self.assertRaisesRegex(b.BundleError, "does not bind"):
            b.check_compatibility(dict(files, **{"image-selection.json":selection+b" "}), self.commit, images)

    def test_frozen_tools_match_the_reviewed_source_tree(self):
        self.lock["tools"]["helm"]["sha256"]["v1.0.0/linux-amd64"] = "0"*64
        with self.assertRaisesRegex(b.BundleError, "tools differ"):
            self.build()

    def test_chart_integrity_including_embedded_dependencies(self):
        for extra in ({"kube-prometheus-stack/../outside": b"bad"},
                      {"kube-prometheus-stack/link": None},
                      {"kube-prometheus-stack/charts/dep-1.tgz": chart({"dep/link": None})}):
            bad = chart({"kube-prometheus-stack/Chart.yaml": b"name: kube-prometheus-stack\nversion: 1\n", **extra})
            with self.assertRaisesRegex(b.BundleError, "unsafe"):
                b.check_chart(bad, {"name":"kube-prometheus-stack", "version":"1", "sha256":b.digest(bad)})
        missing = chart({"demo/Chart.yaml": b"name: demo\nversion: 1\ndependencies: [{name: missing, version: '1'}]\n"})
        with self.assertRaisesRegex(b.BundleError, "packaged dependency"):
            b.check_chart(missing, {"name":"demo", "version":"1", "sha256":b.digest(missing)})
        self.row["chart"]["sha256"] = "1"*64
        with self.assertRaisesRegex(b.BundleError, "SHA-256"):
            self.build()

    def test_missing_or_extra_source_members_never_enter_bundle(self):
        (self.source / ".env").write_text("not meant for the bundle")
        with self.assertRaisesRegex(b.BundleError, "unexpected"):
            self.build()
        (self.source / ".env").unlink()
        (self.base / "values.yaml").unlink()
        (self.base / "values.yaml").symlink_to(self.base / "rendered.yaml")
        with self.assertRaises(OSError):
            self.build()
        self.assertEqual([], list(self.dest.iterdir()))

    def test_release_observation_recovery_and_prerequisites_are_required(self):
        self.row["needs"] = ["default/jaeger"]
        with self.assertRaisesRegex(b.BundleError, "prerequisite"):
            self.build()
        self.row["needs"] = []
        self.live["releases"][self.key]["status"] = "pending-upgrade"
        with self.assertRaisesRegex(b.BundleError, "operator review"):
            self.build()
        self.live["releases"][self.key]["status"] = "deployed"
        self.live["rollbackTargets"][self.key]["recordSHA256"] = "0"*64
        with self.assertRaisesRegex(b.BundleError, "Rollback target"):
            self.build()
        self.live["rollbackTargets"][self.key]["recordSHA256"] = self.record_sha
        self.row["observe"] = ["v1:Secret:default:other"]
        self.live["objects"] = {self.row["observe"][0]: None}
        with self.assertRaisesRegex(b.BundleError, "rendered resource"):
            self.build()

    def test_insecure_keys_and_plaintext_working_directories_fail(self):
        key = self.root / "key"
        key.write_bytes(Fernet.generate_key())
        key.chmod(0o640)
        with self.assertRaisesRegex(b.BundleError, "owner-only"):
            b.read_key(key)
        self.dest.chmod(0o755)
        with self.assertRaisesRegex(b.BundleError, "owner-only"):
            self.build()
