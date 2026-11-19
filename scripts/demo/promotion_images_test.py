# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import copy
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

import promotion_images as p


def raw(data):
    return json.dumps(data, sort_keys=True).encode()


def digest(data):
    return "sha256:" + sha256(data).hexdigest()


def registry_fixture():
    cfg = raw({"os": "linux", "architecture": "amd64", "config": {}})
    child = raw({"schemaVersion": 2, "mediaType": sorted(p.MANIFEST)[0], "config": {"digest": digest(cfg)}})
    index = raw({"schemaVersion": 2, "mediaType": sorted(p.INDEX)[0], "manifests": [
        {"platform": {"os": "linux", "architecture": "amd64"}, "digest": digest(child)}]})
    return cfg, child, index


def source(name):
    repo = p.REPOS[name]
    tag = ("a" if name == "jaeger" else "b") * 40
    cfg, child, index = registry_fixture()
    return {"repository": repo, "tag": tag, "digest": digest(index), "sourceCommit": tag,
            "reference": f"docker.io/{repo}:{tag}@{digest(index)}", "platformManifest": digest(child)}


def selection():
    src = {n: source(n) for n in p.REPOS}
    values = {n: {"repository": s["repository"], "tag": s["tag"] + "@" + s["digest"], "pullPolicy": "Always"}
              for n, s in src.items()}
    return {"mode": "promotion", "configCommit": "c" * 40, "coreMain": "d" * 40, "platform": "linux/amd64",
            "sources": src, "images": {n: values for n in p.STACKS}}


def render(stack, src):
    ns = p.STACKS[stack]
    config = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "config", "namespace": ns},
              "data": {"user-config.yaml": f"example: {stack}\n"}}
    ui = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "ui-config", "namespace": ns},
          "data": {"ui-config.json": '{"web": {}}'}}
    backend = {"name": "jaeger", "args": ["--config", "/etc/jaeger/user-config.yaml"],
               "image": src["jaeger"]["reference"], "env": [{"name": "INSTANCE", "value": stack}],
               "volumeMounts": [{"name": "c", "mountPath": "/etc/jaeger"}, {"name": "u", "mountPath": "/etc/jaeger/ui-config"}]}
    pod = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "jaeger"},
           "spec": {"template": {"spec": {"containers": [backend], "volumes": [
               {"name": "c", "configMap": {"name": "config"}}, {"name": "u", "configMap": {"name": "ui-config"}}]}}}}
    hotrod = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "jaeger-hotrod"},
              "spec": {"template": {"spec": {"containers": [{"name": "jaeger-hotrod", "image": src["hotrod"]["reference"]}]}}}}
    return yaml.safe_dump_all([config, ui, pod, hotrod]).encode()


class RegistryTests(unittest.TestCase):
    def test_manifest_index_child_blob_and_pull_scope(self):
        tag = "a" * 40
        cfg, child, index = registry_fixture()
        requests = []

        def fetch(url, headers=None):
            requests.append((url, headers))
            if url.startswith("https://auth.docker.io/token?"):
                self.assertIn("jaegertracing%2Fjaeger-snapshot%3Apull", url)
                return raw({"token": "pull-only"}), {}
            self.assertEqual(headers["Authorization"], "Bearer pull-only")
            body = cfg if "/blobs/" in url else (child if url.endswith(digest(child)) else index)
            return body, {"Docker-Content-Digest": digest(body)}

        with patch.object(p, "fetch", side_effect=fetch):
            img = p.registry_image(p.REPOS["jaeger"], tag)
            self.assertEqual(img["platformManifest"], digest(child))
            self.assertEqual(img["digest"], digest(index))
            requests.clear()
            self.assertEqual(p.registry_image(p.REPOS["jaeger"], tag, frozen=img), img)
            self.assertFalse(any(url.endswith("/" + tag) for url, _ in requests))

    def test_pinned_live_reference_uses_immutable_bytes_and_verifies_unlabelled_commit_tag(self):
        tag = "a" * 40
        cfg, child, index = registry_fixture()
        def response(data):
            return data, {"Docker-Content-Digest": digest(data)}
        replies = [(raw({"token": "fixture"}), {}), response(index), response(child), (cfg, {})]
        with patch.object(p, "fetch", side_effect=[*replies, response(index)]) as fetch:
            src = p.registry_image(p.REPOS["jaeger"], tag, reference_digest=digest(index))
            self.assertEqual(src["digest"], digest(index))
            self.assertTrue(fetch.call_args_list[1].args[0].endswith(digest(index)))
            self.assertTrue(fetch.call_args_list[-1].args[0].endswith(tag))
        with patch.object(p, "fetch", side_effect=[*replies, response(child)]):
            with self.assertRaisesRegex(ValueError, "claimed commit"):
                p.registry_image(p.REPOS["jaeger"], tag, reference_digest=digest(index))

    def test_rejects_registry_digest_or_platform_mismatch(self):
        cfg, child, index = registry_fixture()
        tag = "a" * 40
        token = (raw({"token": "fixture"}), {})
        response = lambda body: (body, {"Docker-Content-Digest": digest(body)})
        with patch.object(p, "fetch", side_effect=[token, (index, {"Docker-Content-Digest": digest(cfg)})]):
            with self.assertRaisesRegex(ValueError, "manifest digest"):
                p.registry_image(p.REPOS["jaeger"], tag)
        arm = json.loads(index)
        arm["manifests"][0]["platform"]["architecture"] = "arm64"
        with patch.object(p, "fetch", side_effect=[token, response(raw(arm))]):
            with self.assertRaisesRegex(ValueError, "linux/amd64"):
                p.registry_image(p.REPOS["jaeger"], tag)
        with patch.object(p, "fetch", side_effect=[token, response(index), response(child), (b"changed", {})]):
            with self.assertRaisesRegex(ValueError, "configuration content digest"):
                p.registry_image(p.REPOS["jaeger"], tag)
        with patch.object(p, "fetch", side_effect=[token, response(index), response(child), (cfg, {})]):
            frozen = dict(source("jaeger"), platformManifest=digest(cfg))
            with self.assertRaisesRegex(ValueError, "platform manifest"):
                p.registry_image(p.REPOS["jaeger"], tag, frozen=frozen)

    def test_named_tag_uses_digest_verified_source_alias(self):
        commit, root = "e" * 40, "sha256:" + "d" * 64
        base = "https://hub.docker.com/v2/repositories/jaegertracing/jaeger-snapshot/tags/"
        pages = [{"results": [], "next": base + "?page=2"}, {"results": [{"digest": root, "name": commit}]}]
        lookup = Mock(return_value=({}, root))
        with patch.object(p, "fetch", side_effect=[(raw(page), {}) for page in pages]):
            self.assertEqual(p.tag_commit(p.REPOS["jaeger"], "human-tag", root, {}, lookup), commit)
        lookup.assert_called_once_with(commit)
        with self.assertRaisesRegex(ValueError, "differs"):
            p.tag_commit(p.REPOS["jaeger"], "human-tag", root, {"org.opencontainers.image.revision": commit},
                         lambda unused: ({}, digest(b"changed")))


class SelectionTests(unittest.TestCase):
    def test_resolver_uses_core_main_and_propagates_only_explicit_overrides(self):
        sources = {k: source(k) for k in p.REPOS}

        def run(cmd, **kwargs):
            if cmd[0] == "git":
                self.assertEqual(cmd, ["git", "ls-remote", p.CORE, "refs/heads/main"])
                return "d" * 40 + "\trefs/heads/main\n"
            env = kwargs["env"]
            self.assertEqual(env["MAIN_SHA"], "d" * 40)
            self.assertEqual(env["GITHUB_EVENT_NAME"], "schedule")
            self.assertNotIn("GITHUB_SHA", env)
            self.assertNotIn("JAEGER_DEMO_HOTROD_IMAGE_TAG", env)
            self.assertEqual(env["JAEGER_DEMO_JAEGER_IMAGE_TAG"], "my-tag")
            Path(env["GITHUB_ENV"]).write_text("".join("JAEGER_DEMO_" + n.upper() + "_IMAGE_TAG=" + d["tag"] + "\n"
                                                      for n, d in sources.items()))
            return ""

        with patch.object(p, "run", side_effect=run), patch.object(p, "config_commit", return_value="c" * 40), \
                patch.object(p, "registry_image", side_effect=sources.values()):
            selected = p.resolve("schedule", {"jaeger": "my-tag", "hotrod": None})
        self.assertEqual(selected["images"]["otel"]["jaeger"]["tag"], sources["jaeger"]["tag"] + "@" + sources["jaeger"]["digest"])
        self.assertEqual(selected["sources"], sources)
        self.assertNotEqual(selected["configCommit"], selected["coreMain"])

    def test_failed_second_image_resolution_never_publishes_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "selection.json"
            args = ["cmd", "resolve", "--event", "push", "--output", str(out)]
            with patch("sys.argv", args), patch.object(p, "resolve", side_effect=ValueError("unpublished HotROD")):
                with self.assertRaisesRegex(ValueError, "unpublished"):
                    p.main()
            self.assertFalse(out.exists())


class CompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.dat = selection()
        self.selection = raw(self.dat)
        self.renders = {n: render(n, self.dat["sources"]) for n in p.STACKS}

    def test_bad_plan_or_render_stops_before_any_runtime_pull(self):
        with patch.object(p, "config_commit", return_value=self.dat["configCommit"]), patch.object(p, "run") as runtime:
            for mutate, reason in [
                (lambda d: d.update(mode="adoption"), "Invalid promotion"),
                (lambda d: d["images"].pop("otel"), "Incomplete image"),
                (lambda d: d["sources"]["hotrod"].update(reference="other"), "Invalid snapshot"),
                (lambda d: d["images"]["oci"]["jaeger"].update(tag="latest"), "Unexpected Helmfile"),
            ]:
                modified = copy.deepcopy(self.dat)
                mutate(modified)
                data = raw(modified)
                with self.subTest(reason=reason), self.assertRaisesRegex(ValueError, reason):
                    p.validate(data, p.sha(data), self.renders, "podman")
            with self.assertRaisesRegex(ValueError, "reviewed SHA"):
                p.validate(self.selection, "0" * 64, self.renders, "podman")
            with self.assertRaisesRegex(ValueError, "differ"):
                wrong = self.renders["otel"].replace(self.dat["sources"]["hotrod"]["reference"].encode(), b"mutable:latest")
                p.validate(self.selection, p.sha(self.selection), dict(self.renders, otel=wrong), "podman")
            with self.assertRaisesRegex(ValueError, "exactly one"):
                p.validate(self.selection, p.sha(self.selection), dict(self.renders, otel=self.renders["oci"]), "podman")
            runtime.assert_not_called()

    @patch.object(p, "registry_image", side_effect=lambda repo, tag, frozen: frozen)
    def test_runs_two_exact_configs_with_selected_binary(self, checked):
        stack_files = []

        def run(cmd, **kwargs):
            if cmd[1] == "pull":
                self.assertEqual(cmd[2:4], ["--platform", "linux/amd64"])
                return ""
            self.assertEqual(cmd[-3:], ["validate", "--config", "/etc/jaeger/user-config.yaml"])
            self.assertEqual(cmd[cmd.index("--network") + 1], "none")
            self.assertEqual(cmd[-4], "docker.io/" + p.REPOS["jaeger"] + "@" + self.dat["sources"]["jaeger"]["digest"])
            path = cmd[cmd.index("--mount") + 1].split("source=")[1].split(",target")[0]
            stack_files.append((Path(path) / "user-config.yaml").read_text())
            self.assertEqual((Path(path) / "ui-config/ui-config.json").read_text(), '{"web": {}}')

        with patch.object(p, "config_commit", return_value=self.dat["configCommit"]), patch.object(p, "run", side_effect=run):
            receipt = p.validate(self.selection, p.sha(self.selection), self.renders, "podman")
        self.assertEqual(stack_files, ["example: oci\n", "example: otel\n"])
        self.assertEqual(receipt["selectionSHA256"], p.sha(self.selection))
        self.assertEqual(receipt["checks"]["otel"]["renderSHA256"], p.sha(self.renders["otel"]))
        self.assertEqual(checked.call_count, 2)

    @patch.object(p, "registry_image", side_effect=lambda repo, tag, frozen: frozen)
    def test_second_stack_incompatibility_leaves_no_receipt(self, _checked):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            selection_file, oci, otel, receipt = (directory / n for n in ["selection", "oci", "otel", "output"])
            selection_file.write_bytes(self.selection)
            oci.write_bytes(self.renders["oci"])
            otel.write_bytes(self.renders["otel"])
            args = ["cmd", "validate", "--selection", str(selection_file), "--selection-sha256", p.sha(self.selection),
                    "--oci-render", str(oci), "--otel-render", str(otel), "--output", str(receipt)]
            with patch("sys.argv", args), patch.object(p, "config_commit", return_value=self.dat["configCommit"]), \
                    patch.object(p, "run", side_effect=["pull", "pass", subprocess.CalledProcessError(1, "validate")]):
                with self.assertRaisesRegex(ValueError, "otel configuration"):
                    p.main()
            self.assertFalse(receipt.exists())
            p.publish(receipt, {"existing": True})
            with self.assertRaises(FileExistsError):
                p.publish(receipt, {"changed": True})
            self.assertEqual(json.loads(receipt.read_bytes()), {"existing": True})
