# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import helmfile_tools as tools


def archive(member, content, link=False):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as out:
        info = tarfile.TarInfo(member)
        if link:
            info.type = tarfile.SYMTYPE
            info.linkname = "elsewhere"
            out.addfile(info)
        else:
            info.size = len(content)
            out.addfile(info, io.BytesIO(content))
    return buf.getvalue()


class ToolchainTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.data = archive("linux-amd64/helm", b"fixture")
        self.digest = hashlib.sha256(self.data).hexdigest()
        self.pin = {"version": "v4.3.2", "repository": "helm/helm",
                    "sha256": {"v4.3.2/linux-amd64": self.digest}}

    def install(self, dirname):
        tools.install("helm", self.pin, "linux-amd64", self.cache, self.root / dirname)

    def test_cold_cache_then_recheck_and_unpack_in_new_location(self):
        def fetch(url, target):
            self.assertEqual(url, "https://get.helm.sh/helm-v4.3.2-linux-amd64.tar.gz")
            target.write_bytes(self.data)
        with patch.object(tools, "download", side_effect=fetch) as request:
            self.install("first")
            (self.root / "first/bin/helm").write_bytes(b"altered installed file")
            self.install("second")
            request.assert_called_once()
        self.assertEqual(b"fixture", (self.root / "second/bin/helm").read_bytes())
        (self.cache / self.digest).write_bytes(b"altered cached archive")
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.install("corrupt")
        self.assertFalse((self.root / "corrupt/bin/helm").exists())

    def test_incorrect_download_is_not_cached_and_version_bump_requires_hashes(self):
        with patch.object(tools, "download", side_effect=lambda url, dst: dst.write_bytes(b"bad")):
            with self.assertRaisesRegex(ValueError, "does not match"):
                self.install("bad")
        self.assertEqual([], list(self.cache.iterdir()))
        self.pin["version"] = "v4.3.3"
        with self.assertRaisesRegex(ValueError, "Missing reviewed SHA-256"):
            self.install("old")

    def test_plugin_or_binary_member_must_be_a_regular_file(self):
        data = archive("linux-amd64/helm", b"", link=True)
        digest = hashlib.sha256(data).hexdigest()
        (self.cache / digest).write_bytes(data)
        self.pin["sha256"]["v4.3.2/linux-amd64"] = digest
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.install("link")

    def test_refresh_checks_every_platform_in_official_manifest(self):
        pin = {"version": "v0.8.0", "repository": "yannh/kubeconform"}
        lines = [f"{self.digest}  kubeconform-{system}.tar.gz" for system in tools.PLATFORMS]
        def fetch(url, target):
            self.assertEqual(url, "https://github.com/yannh/kubeconform/releases/download/v0.8.0/CHECKSUMS")
            target.write_text("\n".join(lines))
        with patch.object(tools, "download", side_effect=fetch) as request:
            result = tools.refreshed_checksums("kubeconform", pin)
            self.assertEqual({f"v0.8.0/{system}": self.digest for system in tools.PLATFORMS}, result)
            request.assert_called_once()
            lines.pop()
            with self.assertRaisesRegex(ValueError, "Missing or ambiguous"):
                tools.refreshed_checksums("kubeconform", pin)
