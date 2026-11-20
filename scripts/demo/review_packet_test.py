# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import copy
import json
from pathlib import Path
import tempfile
import unittest
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ops/executor"))
import review_packet as packet

UID = "00000000-0000-4000-8000-000000000001"
IMAGE = "ghcr.io/jaegertracing/jaeger-demo-executor@sha256:" + "a" * 64
ACTORS = {
    "bootstrap": "operator-for-test",
    "recovery": "recovery-for-test",
    "requester": "requester-for-test",
    **{
        key: f"system:serviceaccount:{packet.permissions.OPS}:{val}"
        for key, val in (("preparer", packet.permissions.PREPARER), ("executor", packet.permissions.EXECUTOR))
    },
}


class ReviewPacketTests(unittest.TestCase):
    def test_partial_packet_opens_no_cluster_and_each_byte_hash_and_mode_is_reproducible(self):
        with tempfile.TemporaryDirectory() as d:
            outputs = []
            for n in ("first", "second"):
                dest = Path(d) / n
                index, checksum = packet.save(dest, ACTORS, IMAGE, "fixture-rwo", UID)
                self.assertEqual(dest.stat().st_mode & 0o777, 0o700)
                self.assertEqual(checksum, packet.sha((dest / "review.json").read_bytes()))
                self.assertEqual(index["duplicateIdentitiesRemoved"], 1)
                self.assertFalse(index["allManifestPhasesIncluded"])
                self.assertEqual(
                    [s["phase"] for s in index["stages"] if not s["objects"]],
                    ["14-monitoring-guards", "25-monitoring-grants"],
                )
                ids = set()
                for stage in index["stages"]:
                    if stage["objects"]:
                        file = dest / stage["file"]
                        self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                        raw = file.read_bytes()
                        self.assertEqual(packet.sha(raw), stage["sha256"])
                        data = json.loads(raw)["items"]
                        self.assertEqual(len(data), stage["objects"])
                        for o in data:
                            identity = packet.identity(o)
                            self.assertNotIn(identity, ids)
                            ids.add(identity)
                outputs.append(checksum)
            self.assertEqual(*outputs)
            with self.assertRaises(FileExistsError):
                packet.save(dest, ACTORS, IMAGE, "fixture-rwo", UID)

    def test_conflict_unresolved_reference_activation_and_actor_mismatch(self):
        base = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "fixture"}, "data": {"a": "b"}}
        diff = copy.deepcopy(base)
        diff["data"] = {}
        with self.assertRaisesRegex(ValueError, "conflicting"):
            packet.normalize({"a": [base], "b": [diff]})
        with self.assertRaisesRegex(ValueError, "precedes"):
            packet.normalize(
                {
                    "x": [
                        {
                            "apiVersion": packet.permissions.ADMISSION,
                            "kind": "ValidatingAdmissionPolicyBinding",
                            "metadata": {"name": "fixture"},
                            "spec": {"policyName": "missing"},
                        }
                    ]
                }
            )
        with self.assertRaisesRegex(ValueError, "disabled"):
            packet.normalize(
                {
                    "x": [
                        {
                            "apiVersion": "apps/v1",
                            "kind": "Deployment",
                            "metadata": {"name": "fixture"},
                            "spec": {"replicas": 1},
                        }
                    ]
                }
            )
        for actors in (
            dict(ACTORS, bootstrap=ACTORS["requester"]),
            dict(ACTORS, preparer="unexpected"),
            dict(ACTORS, requester="*"),
        ):
            with self.assertRaises(ValueError):
                packet.build(actors, IMAGE, "fixture-rwo", UID, None)

    def test_hook_pair_requires_two_independently_reviewed_digests_and_cluster(self):
        with tempfile.TemporaryDirectory() as d:
            directory = Path(d)
            cfg = {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"namespace": packet.permissions.OPS, "name": "pinned-chart"},
                "immutable": True,
            }
            groups = [
                [cfg],
                packet.permissions.bindings("fixture-hook", "cluster", [], (packet.permissions.EXECUTOR,)),
            ]
            attestation = {"clusterUID": UID}
            for phase, objs in zip(("guards", "grants"), groups):
                raw = packet.encoded({"apiVersion": "v1", "kind": "List", "items": objs})
                (directory / (phase + ".json")).write_bytes(raw)
                attestation[phase + "SHA256"] = packet.sha(raw)
            self.assertEqual(packet.reviewed_hooks(directory, attestation, UID), groups)
            with self.assertRaisesRegex(ValueError, "cluster UID"):
                packet.reviewed_hooks(directory, dict(attestation, clusterUID="different"), UID)
            (directory / "guards.json").write_bytes(b"{}")
            with self.assertRaisesRegex(ValueError, "digest"):
                packet.reviewed_hooks(directory, attestation, UID)
            with self.assertRaisesRegex(ValueError, "together"):
                packet.reviewed_hooks(None, attestation, UID)

    def test_rejects_packet_output_inside_checkout(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            packet.save(packet.ROOT / "must-not-be-written", ACTORS, IMAGE, "fixture-rwo", UID)
