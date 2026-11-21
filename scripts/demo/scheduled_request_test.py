# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import contextlib
import copy
from datetime import timedelta
import io
import json
from unittest.mock import patch
import unittest

import scheduled_request as req


class Store:
    def __init__(self):
        self.rows = {}
        self.initial(req.REQUEST, {"schema": req.SCHEMA, "state": "empty"})
        self.initial(req.CHECKPOINT, {}, annotations={req.PROTOCOL: "v1"})

    def initial(self, key, data, annotations=None):
        ns, name = key
        self.rows[key] = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "namespace": ns,
                "name": name,
                "uid": "uid-" + name,
                "resourceVersion": "1",
                "annotations": annotations or {},
            },
            "data": data,
        }

    def command(self, *parts):
        assert parts == ("auth", "whoami", "-o", "json")
        return json.dumps({"status": {"userInfo": {"username": "human-recovery"}}})

    def read(self, key):
        return copy.deepcopy(self.rows.get(key))

    def patch(self, key, observed, ops):
        obj = self.rows[key]
        if (
            obj["metadata"]["uid"] != observed["metadata"]["uid"]
            or obj["metadata"]["resourceVersion"] != observed["metadata"]["resourceVersion"]
        ):
            raise req.RequestError("conflict")
        for action in ops:
            path = [x.replace("~1", "/").replace("~0", "~") for x in action["path"].split("/")[1:]]
            node = obj
            for k in path[:-1]:
                node = node[k]
            k = path[-1]
            if action["op"] == "test":
                if node[k] != action["value"]:
                    raise req.RequestError("conflict")
            elif action["op"] == "remove":
                del node[k]
            else:
                node[k] = copy.deepcopy(action["value"])
        obj["metadata"]["resourceVersion"] = str(int(obj["metadata"]["resourceVersion"]) + 1)


class ScheduledRequestTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.slot = req.timestamp("2026-11-18T13:00:00Z")
        self.expires = self.slot + timedelta(hours=2)
        self.receipt = {"configurationSHA": "c" * 40, "bundleSHA256": "b" * 64}
        self.sha = self.receipt["configurationSHA"]
        self.now = self.slot
        self.prepare()

    def prepare(self):
        req.prepare(self.store, self.slot, self.expires, "scripts-v1", self.receipt, self.now)

    def validate(self):
        return req.validate(self.store.read(req.REQUEST), self.slot, "scripts-v1", self.sha)

    def signal(self):
        req.signal(self.store, self.store.read(req.REQUEST), self.now)

    def execute(self, apply=lambda receipt: True):
        return req.execute(self.store, self.slot, self.sha, self.receipt, apply, now=lambda: self.now)

    def test_whole_transaction_is_idempotent_and_shared_lock_spans_health_verification(self):
        seen = []

        def apply(receipt):
            seen.append(receipt)
            self.assertEqual(self.validate()["state"], "running")
            with self.assertRaisesRegex(req.RequestError, "held"):
                req.acquire(self.store, "manual/123/1")
            return True

        self.assertEqual(
            req.request_and_wait(
                self.store, self.slot, self.sha, now=lambda: self.now, sleep=lambda _: self.execute(apply)
            ),
            self.receipt,
        )
        self.assertEqual(seen, [self.receipt])
        self.assertEqual(self.validate()["state"], "succeeded")
        self.assertNotIn(req.HOLDER, self.store.read(req.CHECKPOINT)["metadata"]["annotations"])
        self.prepare()
        self.assertEqual(
            req.request_and_wait(
                self.store,
                self.slot,
                self.sha,
                now=lambda: self.now,
                sleep=lambda _: self.fail("unexpected repeat execution"),
            ),
            self.receipt,
        )
        with self.assertRaisesRegex(req.RequestError, "already claimed"):
            self.execute()

    def test_manual_work_blocks_receiver_including_when_cron_is_paused(self):
        self.assertTrue(req.acquire(self.store, "manual/1/1", required=False))
        self.signal()
        with self.assertRaisesRegex(req.RequestError, "held"):
            self.execute()
        with self.assertRaisesRegex(req.RequestError, "own"):
            req.release(self.store, "manual/2/1")
        req.release(self.store, "manual/1/1")
        self.execute()

    def test_failed_verify_holds_lock_for_recovery(self):
        self.signal()
        with self.assertRaisesRegex(req.RequestError, "verify"):
            self.execute(lambda receipt: False)
        self.assertEqual("failed", self.validate()["state"])
        with self.assertRaisesRegex(req.RequestError, "held"):
            req.acquire(self.store, "manual/2/1", required=False)
        with self.assertRaisesRegex(req.RequestError, "recovery"):
            req.prepare(
                self.store,
                self.slot + timedelta(days=1),
                self.expires + timedelta(days=1),
                "scripts-v1",
                self.receipt,
                self.slot + timedelta(days=1),
            )
        with self.assertRaisesRegex(req.RequestError, "reported failure"):
            req.request_and_wait(self.store, self.slot, self.sha, now=lambda: self.now)

    def snapshot(self):
        return tuple(
            (self.store.read(ref)["metadata"]["uid"], self.store.read(ref)["metadata"]["resourceVersion"])
            for ref in (req.REQUEST, req.CHECKPOINT)
        )

    def recover(self, reviewed, receipt=None, confirmed=True, operator="human-recovery"):
        req.recover(
            self.store, self.slot, "scripts-v1", receipt or self.receipt, reviewed, operator, confirmed
        )

    def test_recovery_requires_a_confirmed_matching_operation_and_preserves_the_failed_result(self):
        self.signal()
        with self.assertRaisesRegex(req.RequestError, "verify"):
            self.execute(lambda _: False)
        reviewed = self.snapshot()
        bad_receipt = dict(self.receipt, bundleSHA256="a" * 64)
        for kwargs, error in (
            ({"confirmed": False}, "Confirm"),
            ({"operator": "requester"}, "identity"),
            ({"receipt": bad_receipt}, "Only the reviewed"),
            ({"reviewed": (("wrong", reviewed[0][1]), reviewed[1])}, "changed since"),
            ({"reviewed": (reviewed[0], (reviewed[1][0], "old"))}, "changed since"),
        ):
            with self.subTest(error=error), self.assertRaisesRegex(req.RequestError, error):
                self.recover(**{"reviewed": reviewed, **kwargs})
            self.assertEqual(self.snapshot(), reviewed)
        self.recover(reviewed)
        self.assertEqual(self.validate()["state"], "recovered")
        self.assertNotIn(req.HOLDER, self.store.read(req.CHECKPOINT)["metadata"]["annotations"])
        with self.assertRaisesRegex(req.RequestError, "reported failure"):
            req.request_and_wait(self.store, self.slot, self.sha, now=lambda: self.expires)
        with self.assertRaisesRegex(req.RequestError, "already claimed"):
            self.execute()
        with self.assertRaisesRegex(req.RequestError, "Only the reviewed"):
            self.recover(self.snapshot())
        later = self.slot + timedelta(days=1)
        req.prepare(self.store, later, later + timedelta(hours=2), "scripts-v1", self.receipt, later)
        self.assertEqual(self.store.read(req.REQUEST)["data"]["state"], "ready")

    def test_operator_cli_requires_confirmation_and_uses_the_reviewed_records(self):
        self.signal()
        with self.assertRaisesRegex(req.RequestError, "verify"):
            self.execute(lambda _: False)
        request_ref, checkpoint_ref = self.snapshot()
        args = [
            "scheduled_request.py",
            "recover",
            "--operator",
            "human-recovery",
            "--slot",
            req.format_utc(self.slot),
            "--engine",
            "scripts-v1",
            "--receipt",
            "test.json",
            "--request-uid",
            request_ref[0],
            "--request-version",
            request_ref[1],
            "--checkpoint-uid",
            checkpoint_ref[0],
            "--checkpoint-version",
            checkpoint_ref[1],
        ]
        with patch.object(req.sys, "argv", args), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as missing:
                req.main()
            self.assertEqual(missing.exception.code, 2)
        self.assertEqual(self.snapshot(), (request_ref, checkpoint_ref))
        with (
            patch.object(req.sys, "argv", [*args, "--confirmed-stopped-and-reconciled"]),
            patch.object(req, "ConfigMaps", return_value=self.store),
            patch.object(req.Path, "read_bytes", return_value=json.dumps(self.receipt).encode()),
        ):
            req.main()
        self.assertEqual(self.validate()["state"], "recovered")

    def test_recovery_can_resume_a_released_lock_without_touching_a_new_writer(self):
        self.signal()
        with self.assertRaisesRegex(req.RequestError, "verify"):
            self.execute(lambda _: False)
        reviewed = self.snapshot()
        with patch.object(req, "set_state", side_effect=req.RequestError("conflict")):
            with self.assertRaisesRegex(req.RequestError, "conflict"):
                self.recover(reviewed)
        self.assertEqual(self.validate()["state"], "failed")
        self.assertNotIn(req.HOLDER, self.store.read(req.CHECKPOINT)["metadata"]["annotations"])
        with self.assertRaisesRegex(req.RequestError, "changed since"):
            self.recover(reviewed)
        req.acquire(self.store, "manual/2/1")
        with self.assertRaisesRegex(req.RequestError, "lock owner"):
            self.recover(self.snapshot())
        self.assertEqual(
            self.store.read(req.CHECKPOINT)["metadata"]["annotations"][req.HOLDER], "manual/2/1"
        )
        req.release(self.store, "manual/2/1")
        self.recover(self.snapshot())
        self.assertEqual(self.validate()["state"], "recovered")

    def test_original_window_controls_new_signals_and_claims_but_existing_results_remain_observable(self):
        self.signal()
        late = self.expires + timedelta(hours=1)
        with self.assertRaisesRegex(req.RequestError, "did not claim"):
            req.request_and_wait(self.store, self.slot, self.sha, now=lambda: late)
        for state in ("running", "verified"):
            with self.subTest(state=state):
                req.set_state(self.store, self.store.read(req.REQUEST), state)
                with patch.object(req, "signal", side_effect=AssertionError("unexpected new signal")):
                    result = req.request_and_wait(
                        self.store,
                        self.slot,
                        self.sha,
                        now=lambda: late,
                        sleep=lambda _: req.set_state(
                            self.store, self.store.read(req.REQUEST), "succeeded"
                        ),
                    )
                    self.assertEqual(result, self.receipt)
        self.assertEqual(
            req.request_and_wait(self.store, self.slot, self.sha, now=lambda: late), self.receipt
        )
        req.set_state(self.store, self.store.read(req.REQUEST), "failed")
        with self.assertRaisesRegex(req.RequestError, "reported failure"):
            req.request_and_wait(self.store, self.slot, self.sha, now=lambda: late)

    def test_status_requires_lock_release_before_succeeded(self):
        self.signal()

        def apply(receipt):
            self.store.rows[req.CHECKPOINT]["metadata"]["annotations"][req.HOLDER] = "other"
            return True

        with self.assertRaisesRegex(req.RequestError, "own"):
            self.execute(apply)
        self.assertEqual(self.validate()["state"], "verified")

    def test_stale_conflicting_or_unexpected_inputs_are_rejected(self):
        for field, value in (
            ("configurationSHA", "d" * 40),
            ("engine", "helmfile-v1"),
            ("schema", "other"),
            ("bundleSHA256", "malformed"),
            ("slot", "2026-11-20T13:00:00Z"),
            ("expiresAt", "2026-11-18T23:00:00Z"),
            ("extra", "unexpected"),
        ):
            store = copy.deepcopy(self.store)
            store.rows[req.REQUEST]["data"][field] = value
            with self.subTest(field=field), self.assertRaises(req.RequestError):
                req.request_and_wait(store, self.slot, self.sha, now=lambda: self.now)
        with self.assertRaisesRegex(req.RequestError, "conflicting"):
            req.prepare(
                self.store,
                self.slot,
                self.expires,
                "scripts-v1",
                dict(self.receipt, bundleSHA256="e" * 64),
                self.now,
            )
        with self.assertRaisesRegex(req.RequestError, "window"):
            req.request_and_wait(self.store, self.slot, self.sha, now=lambda: self.expires)
        self.signal()
        with self.assertRaisesRegex(req.RequestError, "differs"):
            req.execute(
                self.store,
                self.slot,
                self.sha,
                dict(self.receipt, bundleSHA256="e" * 64),
                lambda _: True,
                now=lambda: self.now,
            )

    def test_a_record_from_another_slot_names_the_missing_preparation(self):
        # On 2026-11-21 the preparer never ran and the requester found the previous day's record.
        today = self.slot + timedelta(days=1)
        with self.assertRaises(req.RequestError) as caught:
            req.request_and_wait(self.store, today, self.sha, now=lambda: today)
        self.assertEqual(
            "No preparation was published for the 2026-11-19T13:00:00Z slot: "
            "the request record still holds 2026-11-18T13:00:00Z",
            str(caught.exception),
        )
        # The same holds for a completed record, which is what a later day actually finds.
        done = copy.deepcopy(self.store)
        done.rows[req.REQUEST]["data"].update(state="succeeded", requestedAt="2026-11-18T13:12:57Z")
        with self.assertRaisesRegex(req.RequestError, "^No preparation was published for the 2026-11-19T13:00:00Z slot"):
            req.request_and_wait(done, today, self.sha, now=lambda: today)
        # Nothing but a well-formed timestamp is repeated from the record.
        for recorded in ("not a timestamp", "2026-11-17T13:00:00Z\nforged", ""):
            store = copy.deepcopy(self.store)
            store.rows[req.REQUEST]["data"]["slot"] = recorded
            with self.subTest(recorded=recorded), self.assertRaises(req.RequestError) as caught:
                req.request_and_wait(store, self.slot, self.sha, now=lambda: self.now)
            self.assertEqual(
                "No preparation was published for the 2026-11-18T13:00:00Z slot: "
                "the request record names no valid slot",
                str(caught.exception),
            )
        # A bootstrapped record that no preparer has written yet.
        empty = copy.deepcopy(self.store)
        empty.rows[req.REQUEST]["data"] = {"schema": req.SCHEMA, "state": "empty"}
        with self.assertRaisesRegex(req.RequestError, "^No preparation has been published: the request record is still empty$"):
            req.request_and_wait(empty, self.slot, self.sha, now=lambda: self.now)
        # A caller that does not pass a daily slot keeps the interval error, and the matching slot still validates.
        with self.assertRaisesRegex(req.RequestError, "schedule interval"):
            req.validate(self.store.read(req.REQUEST), self.slot + timedelta(minutes=1), "scripts-v1", self.sha)
        self.assertEqual(self.validate()["state"], "ready")

    def test_missing_receiver_timeout_and_prepared_next_slot(self):
        def missing(_):
            self.now += req.ACK_TIMEOUT

        with self.assertRaisesRegex(req.RequestError, "did not claim"):
            req.request_and_wait(self.store, self.slot, self.sha, now=lambda: self.now, sleep=missing)
        later = self.slot + timedelta(days=1)
        req.prepare(self.store, later, self.expires + timedelta(days=1), "scripts-v1", self.receipt, later)
        self.assertEqual(
            req.validate(self.store.read(req.REQUEST), later, "scripts-v1", self.sha)["state"], "ready"
        )
        del self.store.rows[req.REQUEST]
        with self.assertRaisesRegex(req.RequestError, "receiver"):
            req.request_and_wait(self.store, self.slot, self.sha, now=lambda: self.now)

    def test_legacy_manual_only_operates_while_lock_protocol_is_absent(self):
        self.store.rows[req.CHECKPOINT]["metadata"]["annotations"] = {}
        self.assertFalse(req.acquire(self.store, "manual/1/1", required=False))
        with self.assertRaisesRegex(req.RequestError, "provisioned"):
            req.acquire(self.store, "manual/1/1")
        self.store.rows[req.CHECKPOINT]["metadata"]["annotations"][req.HOLDER] = "existing"
        with self.assertRaisesRegex(req.RequestError, "provisioned"):
            req.acquire(self.store, "manual/1/1", required=False)

    def test_changed_resource_version_cannot_claim_stale_snapshot(self):
        old = self.store.read(req.REQUEST)
        self.signal()
        with self.assertRaisesRegex(req.RequestError, "conflict"):
            req.set_state(self.store, old, "running")
