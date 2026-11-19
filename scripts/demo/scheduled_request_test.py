# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import copy
from datetime import timedelta
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
