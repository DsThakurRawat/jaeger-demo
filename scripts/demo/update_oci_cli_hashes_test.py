# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Offline tests for scripts/utils/update-oci-cli-hashes.py and the committed hash file."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

import verify_oci_cli

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("update_oci_cli_hashes", ROOT / "scripts/utils/update-oci-cli-hashes.py")
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)

PINS = "# header\n\nzeta==2.0\nAlpha_pkg==1.0.post1\n"
FILES = {"zeta": ["f" * 64, "0" * 64, "f" * 64], "Alpha_pkg": ["a" * 64]}


class Fixture:
    """A fake checkout and PyPI; getter() never touches the network."""

    def __init__(self, temp, pins=PINS, files=FILES):
        self.root = Path(temp)
        (self.root / "scripts/demo").mkdir(parents=True)
        (self.root / helper.CONSTRAINTS).write_text(pins)
        self.files = {name: list(digests) for name, digests in files.items()}
        self.requests = []

    def getter(self, url):
        self.requests.append(url)
        name = url.split("/")[4]
        return json.dumps({"urls": [{"digests": {"sha256": digest}} for digest in self.files[name]]}).encode()

    def run(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = helper.main(list(argv), root=self.root, getter=self.getter)
        return code, out.getvalue(), err.getvalue()

    def text(self):
        return (self.root / helper.HASHES).read_text()


class OciCliHashHelperTests(unittest.TestCase):
    def test_writes_every_published_digest_once_in_pin_order(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(temp)
            code, out, _ = f.run()
            self.assertEqual(0, code)
            self.assertIn("updated", out)
            self.assertEqual(["https://pypi.org/pypi/zeta/2.0/json", "https://pypi.org/pypi/Alpha_pkg/1.0.post1/json"],
                             f.requests)
            self.assertEqual(helper.HEADER + "\n"
                             + "zeta==2.0 \\\n    --hash=sha256:" + "0" * 64 + " \\\n    --hash=sha256:" + "f" * 64 + "\n"
                             + "Alpha_pkg==1.0.post1 \\\n    --hash=sha256:" + "a" * 64 + "\n", f.text())
            self.assertEqual([("zeta", "2.0", ["0" * 64, "f" * 64]), ("Alpha_pkg", "1.0.post1", ["a" * 64])],
                             helper.read_hashes(f.root / helper.HASHES))

    def test_matching_file_is_a_byte_identical_no_op_and_check_reports_drift(self):
        with tempfile.TemporaryDirectory() as temp:
            f = Fixture(temp)
            # --check never creates the file.
            self.assertEqual(1, f.run("--check")[0])
            self.assertFalse((f.root / helper.HASHES).exists())
            self.assertEqual(0, f.run()[0])
            before = f.text()
            self.assertEqual(0, f.run()[0])
            self.assertEqual(0, f.run("--check")[0])
            self.assertEqual(before, f.text())
            # A publisher adds a file to the release, or a pin changes.
            f.files["zeta"].append("b" * 64)
            code, _, err = f.run("--check")
            self.assertEqual(1, code)
            self.assertIn("python3 scripts/utils/update-oci-cli-hashes.py", err)
            self.assertEqual(before, f.text())

    def test_a_release_without_usable_digests_or_a_loose_pin_writes_nothing(self):
        for pins, files, message in (
                (PINS, dict(FILES, zeta=[]), "no usable SHA-256 digests for zeta==2.0"),
                (PINS, dict(FILES, zeta=["F" * 64]), "no usable SHA-256 digests"),
                (PINS, dict(FILES, zeta=["abc"]), "no usable SHA-256 digests"),
                ("zeta>=2.0\n", FILES, "not an exact package==version pin"),
                ("zeta==2.0 --hash=sha256:" + "0" * 64 + "\n", FILES, "not an exact package==version pin"),
                ("--index-url https://example.test/simple\nzeta==2.0\n", FILES, "not an exact package==version pin")):
            with self.subTest(pins=pins, files=files), tempfile.TemporaryDirectory() as temp:
                f = Fixture(temp, pins, files)
                for argv in ((), ("--check",)):
                    code, _, err = f.run(*argv)
                    self.assertEqual(1, code)
                    self.assertIn(message, err)
                self.assertFalse((f.root / helper.HASHES).exists())

    def test_only_https_is_fetched_or_followed(self):
        handler = helper.HttpsOnlyRedirects()
        request = helper.urllib.request.Request("https://pypi.org/pypi/zeta/2.0/json")
        with self.assertRaisesRegex(ValueError, "not HTTPS"):
            handler.redirect_request(request, io.BytesIO(), 302, "Found", {}, "http://pypi.org/pypi/zeta/2.0/json")
        with self.assertRaisesRegex(ValueError, "not HTTPS"):
            helper.fetch("http://pypi.org/pypi/zeta/2.0/json")
        self.assertTrue(helper.INDEX.startswith("https://pypi.org/pypi/"))

    def test_hash_file_reader_rejects_options_unpinned_and_unhashed_entries(self):
        good = "zeta==2.0 \\\n    --hash=sha256:" + "0" * 64 + "\n"
        for content in ("--index-url https://example.test/simple\n" + good,
                        "--extra-index-url https://example.test/simple\n" + good,
                        "--find-links https://example.test/\n" + good,
                        "-r other.txt\n" + good,
                        "zeta==2.0\n",
                        "zeta==2.0 \\\n",
                        "zeta>=2.0 \\\n    --hash=sha256:" + "0" * 64 + "\n",
                        "zeta==2.0 \\\n    --hash=md5:" + "0" * 32 + "\n",
                        "zeta==2.0 \\\n    --hash=sha256:" + "0" * 63 + "\n",
                        "zeta==2.0 --hash=sha256:" + "0" * 64 + "\n",
                        good + "https://example.test/zeta-2.0.tar.gz\n",
                        good.rstrip("\n") + " \\\n    --index-url https://example.test/simple\n"):
            with self.subTest(content=content), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / "hashes.txt"
                path.write_text(content)
                with self.assertRaises(ValueError):
                    helper.read_hashes(path)

    def test_committed_hash_file_pins_exactly_the_constraints_and_nothing_else(self):
        # The workflows install the hash file and verify against the constraints, so the two must agree.
        entries = helper.read_hashes(ROOT / helper.HASHES)
        self.assertEqual(helper.read_pins(ROOT / helper.CONSTRAINTS), [(name, version) for name, version, _ in entries],
                         "run: python3 scripts/utils/update-oci-cli-hashes.py")
        # The verification step reads the same pins under normalized names.
        self.assertEqual(verify_oci_cli.read_constraints(),
                         {helper.re.sub(r"[-_.]+", "-", name).lower(): version for name, version, _ in entries})
        for name, _, digests in entries:
            with self.subTest(package=name):
                self.assertTrue(digests)
                self.assertEqual(sorted(set(digests)), digests)
        self.assertTrue((ROOT / helper.HASHES).read_text().startswith(helper.HEADER + "\n"))


if __name__ == "__main__":
    unittest.main()
