# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

import scheduled_bundle as b


class CustodyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = self.root / "store"
        self.store.mkdir(mode=0o700)
        self.keys = self.root / "operator-keys"
        b.generate_keys(self.keys)
        self.prepare = {"signing_key": self.keys / "signing-key.pem", "recipient_key": self.keys / "recipient-public.pem"}
        self.verify = {"verification_key": self.keys / "verification-key.pem"}
        self.read = {**self.verify, "decryption_key": self.keys / "decryption-key.pem"}
        self.slot = "2026-11-19T13:00:00Z"
        self.tomorrow = "2026-11-20T13:00:00Z"
        self.sha = "a" * 40
        self.files = {name: ("private rehearsal data: " + name).encode() for name in b.MEMBERS}

    def publish(self, slot=None, sha=None, files=None):
        return b.publish(self.store, slot or self.slot, sha or self.sha, self.files if files is None else files, **self.prepare)

    def test_immutable_ciphertext_and_independent_public_receipt(self):
        with self.assertRaises(b.SlotNotFound):
            b.trusted_receipt(self.store, self.slot, **self.verify)
        receipt = self.publish()
        self.assertEqual(set(receipt), {"configurationSHA", "bundleSHA256"})
        self.assertEqual(receipt["configurationSHA"], self.sha)
        self.assertEqual(b.trusted_receipt(self.store, self.slot, **self.verify), receipt)
        self.assertEqual(b.load(self.store, self.slot, receipt, **self.read), self.files)
        path = self.store / (receipt["bundleSHA256"] + ".hpke")
        self.assertEqual(b.digest(path.read_bytes()), receipt["bundleSHA256"])
        for p in self.store.iterdir():
            self.assertNotIn(b"private rehearsal data", p.read_bytes())
        self.assertEqual(path.stat().st_mode & 0o777, 0o440)
        self.assertEqual(self.publish(), receipt)
        for kwargs in ({"sha": "e" * 40}, {"files": dict(self.files, **{"plan.json": b"changed private data"})}):
            with self.assertRaisesRegex(b.CustodyError, "already owns"):
                self.publish(**kwargs)
        self.assertNotEqual(receipt["bundleSHA256"], self.publish(slot=self.tomorrow)["bundleSHA256"])

    def test_concurrent_publishers_agree_on_one_durable_result(self):
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda _: self.publish(), range(6)))
        self.assertTrue(all(r == results[0] for r in results))
        self.assertEqual(len(list(self.store.glob("*.hpke"))), 1)
        self.assertEqual(len(list(self.store.glob("*.slot"))), 1)
        self.assertFalse(list(self.store.glob(".pending-*")))

    def test_identities_are_separate_and_receiver_can_read_only(self):
        receipt = self.publish()
        self.prepare["signing_key"].unlink()
        self.prepare["recipient_key"].unlink()
        self.store.chmod(0o550)
        try:
            self.assertEqual(b.load(self.store, self.slot, receipt, **self.read), self.files)
        finally:
            self.store.chmod(0o700)
        wrong = self.root / "another-operator"
        b.generate_keys(wrong)
        for keys in ({**self.read, "verification_key": wrong / "verification-key.pem"},
                     {**self.read, "decryption_key": wrong / "decryption-key.pem"},
                     {**self.read, "decryption_key": wrong / "recipient-public.pem"}):
            with self.assertRaises(b.CustodyError):
                b.load(self.store, self.slot, receipt, **keys)
        with self.assertRaises(FileExistsError):
            b.generate_keys(self.keys)

    def test_missing_store_or_key_is_distinct_from_unpublished_slot(self):
        for kwargs in ({"store": self.root / "absent", **self.verify},
                       {"store": self.store, "verification_key": self.root / "absent"}):
            with self.assertRaises(FileNotFoundError):
                b.trusted_receipt(slot=self.slot, **kwargs)
        receipt = self.publish()
        (self.store / (receipt["bundleSHA256"] + ".hpke")).unlink()
        with self.assertRaises(FileNotFoundError):
            b.trusted_receipt(self.store, self.slot, **self.verify)
        with self.assertRaises(FileNotFoundError):
            self.publish()

    def test_reject_cross_slot_or_changed_digest_before_decryption(self):
        receipt = self.publish()
        source = self.store / b.slot_name(self.slot)
        other = self.store / b.slot_name(self.tomorrow)
        other.write_bytes(source.read_bytes())
        with self.assertRaises(b.CustodyError):
            b.load(self.store, self.tomorrow, receipt, **self.read)
        with self.assertRaises(b.CustodyError):
            b.load(self.store, self.slot, dict(receipt, bundleSHA256="f" * 64), **self.read)
        data = json.loads(source.read_bytes())
        data["claim"]["configurationSHA"] = "f" * 40
        source.chmod(0o600)
        source.write_bytes(b.canonical(data))
        with self.assertRaises(b.CustodyError):
            b.trusted_receipt(self.store, self.slot, **self.verify)

    def test_ciphertext_corruption_or_alias_is_refused(self):
        receipt = self.publish()
        path = self.store / (receipt["bundleSHA256"] + ".hpke")
        original = path.read_bytes()
        path.chmod(0o600)
        path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        with self.assertRaisesRegex(b.CustodyError, "digest mismatch"):
            b.load(self.store, self.slot, receipt, **self.read)
        other = self.root / "redirect"
        other.write_bytes(original)
        path.unlink()
        path.symlink_to(other)
        with self.assertRaises(OSError):
            b.load(self.store, self.slot, receipt, **self.read)

    def test_valid_signature_cannot_decrypt_ciphertext_bound_to_another_slot(self):
        receipt = self.publish()
        claim = json.loads((self.store / b.slot_name(self.slot)).read_bytes())["claim"]
        claim["slot"] = self.tomorrow
        signer = b.key_file(self.prepare["signing_key"], b.ed25519.Ed25519PrivateKey, private=True)
        signed = b.canonical({"claim": claim, "signature": signer.sign(b.canonical(claim)).hex()})
        (self.store / b.slot_name(self.tomorrow)).write_bytes(signed)
        with self.assertRaisesRegex(b.CustodyError, "authentication failed"):
            b.load(self.store, self.tomorrow, receipt, **self.read)

    def test_projected_key_mounts_and_permissions(self):
        key = self.prepare["signing_key"]
        visible = self.keys / "projected.key"
        generation = self.keys / "..rotation"
        generation.mkdir()
        (self.keys / "..data").symlink_to(generation.name)
        key.rename(generation / key.name)
        visible.symlink_to("..data/" + key.name)
        self.prepare["signing_key"] = visible
        self.publish()
        (generation / key.name).chmod(0o644)
        with self.assertRaisesRegex(b.CustodyError, "exclude other"):
            self.publish()
        visible.unlink()
        visible.symlink_to(self.store)
        with self.assertRaisesRegex(b.CustodyError, "leaves"):
            self.publish()
        self.read["verification_key"].chmod(0o666)
        with self.assertRaisesRegex(b.CustodyError, "writers"):
            b.trusted_receipt(self.store, self.slot, **self.verify)
        self.read["verification_key"].chmod(0o600)
        self.store.chmod(0o755)
        with self.assertRaisesRegex(b.CustodyError, "directory"):
            b.trusted_receipt(self.store, self.slot, **self.verify)

    def test_bounded_store_preserves_failed_slot_and_orphans(self):
        receipt = self.publish()
        with patch.object(b, "MAX_STORED", 1):
            self.assertEqual(self.publish(), receipt)
            with self.assertRaisesRegex(b.CustodyError, "full"):
                self.publish(slot=self.tomorrow)
        operation = b.write_once
        def interrupted(fd, name, data):
            if name.endswith(".slot"):
                raise OSError("simulated crash")
            return operation(fd, name, data)
        with patch.object(b, "write_once", side_effect=interrupted), self.assertRaises(OSError):
            self.publish(slot=self.tomorrow)
        self.assertEqual(len(list(self.store.glob("*.hpke"))), 2)
        with self.assertRaises(b.SlotNotFound):
            b.trusted_receipt(self.store, self.tomorrow, **self.verify)
        with patch.object(b, "MAX_STORED", 2), self.assertRaisesRegex(b.CustodyError, "full"):
            self.publish(slot=self.tomorrow)
        self.assertEqual(b.load(self.store, self.slot, receipt, **self.read), self.files)

    def test_before_index_must_complete_for_new_slot_and_is_not_replayed_on_resume(self):
        operations = []
        def publish(callback):
            return b.publish(self.store, self.slot, self.sha, self.files, before_index=callback, **self.prepare)
        def failed(receipt):
            self.assertTrue((self.store / (receipt["bundleSHA256"] + ".hpke")).is_file())
            with self.assertRaises(b.SlotNotFound):
                b.trusted_receipt(self.store, self.slot, **self.verify)
            raise RuntimeError("fixture approval write stopped")
        with self.assertRaises(RuntimeError):
            publish(failed)
        with self.assertRaises(b.SlotNotFound):
            b.trusted_receipt(self.store, self.slot, **self.verify)
        def approved(value):
            operations.append(value)
            return "a" * 64
        result = publish(approved)
        self.assertEqual(publish(failed), result)
        self.assertEqual(operations, [result])
        self.assertEqual(b.approval_commitment(self.store, self.slot, result, **self.verify), "a" * 64)
        with self.assertRaises(b.CustodyError):
            b.approval_commitment(self.store, self.slot, dict(result,bundleSHA256="f"*64), **self.verify)

    def test_fixed_members_no_traversal_or_unbounded_expansion(self):
        for wrong in ({}, dict(self.files, **{"../other": b"x"}), dict(self.files, **{"plan.json": b""})):
            with self.assertRaises(b.CustodyError):
                self.publish(files=wrong)
        with patch.object(b, "MAX_MEMBER", 2), self.assertRaises(b.CustodyError):
            self.publish()
        with patch.object(b, "MAX_ARCHIVE", 10), self.assertRaises(b.CustodyError):
            self.publish()
        for change in ("path", "duplicate", "symlink", "compressed", "trailer"):
            with self.subTest(change=change):
                out = io.BytesIO()
                with zipfile.ZipFile(out, "w") as z:
                    for name in sorted(b.MEMBERS):
                        if name == "plan.json" and change == "duplicate":
                            name = "values/oci-jaeger.json"
                        item = zipfile.ZipInfo("../outside" if change == "path" and name == "plan.json" else name)
                        mode = stat.S_IFLNK if change == "symlink" else stat.S_IFREG
                        item.external_attr = (mode | 0o600) << 16
                        with warnings.catch_warnings():
                            warnings.filterwarnings("ignore", message="Duplicate name")
                            z.writestr(item, b"fixture", compress_type=zipfile.ZIP_DEFLATED if change == "compressed" else zipfile.ZIP_STORED)
                if change == "trailer":
                    out.write(b"trailing")
                with self.assertRaises(b.CustodyError):
                    b.unpack(out.getvalue())
