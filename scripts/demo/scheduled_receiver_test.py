# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import scheduled_receiver as receiver
import scheduled_request as protocol
from scheduled_request_test import Store


class SlotNotFound(ValueError):
    pass


class ReceiverTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.paths = receiver.Paths(root / "bundles", root / "keys", root / "outcomes")
        for path in (self.paths.store, self.paths.keys, self.paths.outcomes):
            path.mkdir()
        self.store = Store()
        self.slot = protocol.timestamp("2026-11-19T13:00:00Z")
        self.time = self.slot
        self.stamp = protocol.format_utc(self.slot)
        self.sha = "a" * 40
        self.receipt = {"configurationSHA": self.sha, "bundleSHA256": "b" * 64}
        self.member = {"fixture-member": b"synthetic-private-content"}
        self.bundle = Mock(SlotNotFound=SlotNotFound)
        self.bundle.trusted_receipt.return_value = self.receipt
        self.bundle.publish.return_value = self.receipt
        self.bundle.load.return_value = self.member
        self.adapter = Mock()
        self.adapter.prepare.return_value = self.member
        self.adapter.apply.return_value = True
        self.main_sha = Mock(return_value=self.sha)

    def now(self):
        return self.time

    def prepare(self):
        return receiver.prepare_slot(
            self.store, self.bundle, self.adapter, self.main_sha, self.paths, self.now
        )

    def signal(self):
        self.prepare()
        protocol.signal(self.store, self.store.read(protocol.REQUEST), self.time)

    def poll(self):
        return receiver.poll(self.store, self.bundle, self.adapter, self.paths, self.now)

    def state(self):
        return self.store.read(protocol.REQUEST)["data"]["state"]

    def test_unpublished_slot_uses_only_trusted_sha_and_preparer_keys(self):
        self.bundle.trusted_receipt.side_effect = SlotNotFound()
        self.assertEqual(self.prepare(), self.receipt)
        self.adapter.prepare.assert_called_once_with(self.stamp, self.sha)
        self.bundle.publish.assert_called_once_with(
            self.paths.store,
            self.stamp,
            self.sha,
            self.member,
            signing_key=self.paths.keys / "signing-key.pem",
            recipient_key=self.paths.keys / "recipient-public.pem",
        )
        self.bundle.trusted_receipt.side_effect = None
        old = self.store.read(protocol.REQUEST)
        self.assertEqual(self.prepare(), self.receipt)
        self.assertEqual(self.store.read(protocol.REQUEST), old)
        self.main_sha.assert_called_once_with()
        self.bundle.publish.assert_called_once()

    def test_corrupt_index_invalid_main_or_late_build_do_not_prepare(self):
        self.bundle.trusted_receipt.side_effect = ValueError("bad signature")
        with self.assertRaises(ValueError):
            self.prepare()
        self.adapter.prepare.assert_not_called()
        self.bundle.trusted_receipt.side_effect = SlotNotFound()
        self.main_sha.return_value = "unreviewed"
        with self.assertRaisesRegex(receiver.ReceiverError, "main configuration"):
            self.prepare()
        self.main_sha.return_value = self.sha
        self.adapter.prepare.side_effect = lambda *_: setattr(self, "time", self.slot + receiver.WINDOW)
        with self.assertRaisesRegex(receiver.ReceiverError, "exceeded"):
            self.prepare()
        self.bundle.publish.assert_not_called()
        with self.assertRaisesRegex(receiver.ReceiverError, "Outside"):
            self.prepare()
        self.assertEqual(self.state(), "empty")

    def test_empty_missing_and_expired_requests_do_not_apply(self):
        self.assertEqual(self.poll(), "idle")
        self.signal()
        self.time += protocol.ACK_TIMEOUT
        self.assertEqual(self.poll(), "expired-unclaimed")
        self.time += protocol.timedelta(days=1)
        self.assertEqual(self.poll(), "expired-unclaimed")
        del self.store.rows[protocol.REQUEST]
        with self.assertRaisesRegex(receiver.ReceiverError, "missing"):
            self.poll()
        self.adapter.apply.assert_not_called()

    def test_untrusted_request_cannot_choose_bundle_or_invoke_adapter(self):
        self.signal()
        self.store.rows[protocol.REQUEST]["data"]["bundleSHA256"] = "e" * 64
        with self.assertRaisesRegex(protocol.RequestError, "independent plan"):
            self.poll()
        self.assertEqual(self.state(), "requested")
        self.assertNotIn(protocol.HOLDER, self.store.read(protocol.CHECKPOINT)["metadata"]["annotations"])
        self.bundle.load.assert_not_called()
        self.adapter.apply.assert_not_called()

    def test_decrypt_and_verify_only_inside_original_claim_and_persist_public_outcomes(self):
        self.signal()

        def apply(files, *, slot, configuration_sha, assert_locked):
            self.assertEqual((files, slot, configuration_sha), (self.member, self.stamp, self.sha))
            self.assertEqual(self.state(), "running")
            assert_locked()
            return True

        self.adapter.apply.side_effect = apply
        self.assertEqual(self.poll(), "succeeded")
        self.bundle.load.assert_called_once_with(
            self.paths.store,
            self.stamp,
            self.receipt,
            verification_key=self.paths.keys / "verification-key.pem",
            decryption_key=self.paths.keys / "decryption-key.pem",
        )
        evidence = [json.loads(p.read_bytes()) for p in self.paths.outcomes.iterdir()]
        self.assertEqual({row["phase"] for row in evidence}, {"started", "verified"})
        for row in evidence:
            self.assertEqual(
                set(row),
                {
                    "schema",
                    "phase",
                    "slot",
                    "requestUID",
                    "configurationSHA",
                    "bundleSHA256",
                    "engine",
                    "recordedAt",
                },
            )
            self.assertEqual(row["bundleSHA256"], self.receipt["bundleSHA256"])
        self.assertEqual(self.poll(), "idle")
        self.adapter.apply.assert_called_once()

    def test_drift_or_deadline_before_durable_verification_retains_lock(self):
        for mutate in ("data", "owner", "time"):
            with self.subTest(mutate=mutate):
                self.store = Store()
                self.signal()
                ticks = [0]

                def apply(*_, **kwargs):
                    if mutate == "data":
                        self.store.rows[protocol.REQUEST]["metadata"]["uid"] = "different"
                    elif mutate == "owner":
                        self.store.rows[protocol.CHECKPOINT]["metadata"]["annotations"][protocol.HOLDER] = (
                            "other"
                        )
                    else:
                        ticks[0] = protocol.EXECUTION_TIMEOUT.total_seconds()
                    kwargs["assert_locked"]()

                self.adapter.apply.side_effect = apply
                with patch.object(receiver.time, "monotonic", side_effect=lambda: ticks[0]):
                    with self.assertRaises((receiver.ReceiverError, protocol.RequestError)):
                        self.poll()
                self.assertNotEqual(self.state(), "succeeded")
                self.assertTrue(list(self.paths.outcomes.glob("*.failed.json")))
                for path in self.paths.outcomes.iterdir():
                    path.unlink()

    def test_hard_application_budget_is_cancelled_on_return_and_preserves_claim_on_expiry(self):
        with patch.object(receiver.threading, "Timer") as timer:
            with receiver.application_deadline():
                seconds, expire = timer.call_args.args
                self.assertEqual(seconds, receiver.APPLICATION_BUDGET.total_seconds())
                with patch.object(receiver.os, "write"), patch.object(receiver.os, "_exit") as stop:
                    expire()
                stop.assert_called_once_with(124)
            timer.return_value.start.assert_called_once()
            timer.return_value.cancel.assert_called_once()

    def test_ambiguous_initial_claim_and_duplicate_start_never_reapply(self):
        self.signal()
        self.store.rows[protocol.REQUEST]["data"]["state"] = "running"
        self.assertEqual(self.poll(), "inspect-running")
        self.store.rows[protocol.REQUEST]["data"]["state"] = "verified"
        self.assertEqual(self.poll(), "inspect-verified")
        self.store.rows[protocol.REQUEST]["data"]["state"] = "requested"
        record = {"slot": self.stamp, **self.receipt}
        receiver.persist_outcome(self.paths.outcomes, "started", record)
        with self.assertRaises(FileExistsError):
            self.poll()
        self.assertEqual(self.poll(), "inspect-failed")
        self.adapter.apply.assert_not_called()

    def test_failed_result_is_safe_to_publish_and_a_restart_cannot_retry(self):
        self.signal()
        self.bundle.load.side_effect = RuntimeError("synthetic-private-content")
        with self.assertRaisesRegex(
            receiver.ReceiverError, "^The daily application or verification failed$"
        ):
            self.poll()
        self.assertEqual(self.poll(), "inspect-failed")
        self.assertIn(protocol.HOLDER, self.store.read(protocol.CHECKPOINT)["metadata"]["annotations"])
        self.assertEqual(
            sorted(p.stem.split(".")[-1] for p in self.paths.outcomes.iterdir()), ["failed", "started"]
        )
        self.adapter.apply.assert_not_called()
        self.bundle.load.assert_called_once()

    def test_readiness_is_fresh_and_public_error_log_excludes_private_diagnostics(self):
        mark = self.paths.outcomes / "ready"
        with (
            patch.object(receiver, "READINESS", mark),
            patch.object(protocol, "utcnow", side_effect=self.now),
        ):
            self.assertFalse(receiver.ready())
            receiver.mark_ready("idle")
            self.assertTrue(receiver.ready())
            self.time += protocol.timedelta(minutes=1)
            self.assertFalse(receiver.ready())
            receiver.mark_ready("applying")
            self.time += protocol.timedelta(hours=2)
            self.assertTrue(receiver.ready())
            self.time += protocol.timedelta(hours=3)
            self.assertFalse(receiver.ready())
            log = io.StringIO()
            with (
                patch.object(receiver, "poll", side_effect=RuntimeError("synthetic-private-content")),
                patch.object(receiver.time, "sleep", side_effect=KeyboardInterrupt),
            ):
                with redirect_stdout(log), self.assertRaises(KeyboardInterrupt):
                    receiver.serve(self.store, self.bundle, self.adapter, self.paths)
            self.assertFalse(receiver.ready())
            self.assertNotIn("synthetic-private-content", log.getvalue())
            self.assertIn("poll-error/RuntimeError", log.getvalue())

    def test_incluster_config_references_rotating_token_and_restores_prior_kubeconfig(self):
        account = self.paths.keys
        (account / "namespace").write_text("other")
        with self.assertRaises(receiver.ReceiverError), receiver.cluster_config(account):
            self.fail()
        (account / "namespace").write_text(receiver.NAMESPACE)
        (account / "ca.crt").write_text("public-ca")
        (account / "token").write_text("synthetic-token")
        with patch.dict(receiver.os.environ, {"KUBECONFIG": "old"}):
            with receiver.cluster_config(account):
                data = Path(receiver.os.environ["KUBECONFIG"]).read_text()
                self.assertNotIn("synthetic-token", data)
                self.assertEqual(
                    json.loads(data)["users"][0]["user"], {"tokenFile": str(account / "token")}
                )
            self.assertEqual(receiver.os.environ["KUBECONFIG"], "old")
        control = Mock()
        control.command.return_value = json.dumps({"status": {"userInfo": {"username": "another"}}})
        with self.assertRaisesRegex(receiver.ReceiverError, "different"):
            receiver.authenticate(control, "serve")
