# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

from datetime import datetime, timedelta, timezone
import json
import unittest
from urllib.parse import parse_qs, urlsplit

import stack_health


def metric(at, value):
    point = {"timestamp": at.isoformat(), "gaugeValue": {"doubleValue": str(value)}}
    return {"metrics": [{"metricPoints": [point]}]}


def trace(service, at):
    span = {"traceId": "e1" * 16, "startTimeUnixNano": str(int(at.timestamp() * 1e9))}
    batch = {"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
             "scopeSpans": [{"spans": [span]}]}
    return json.dumps({"result": {"resourceSpans": [batch]}}).encode()


class StackHealthTests(unittest.TestCase):
    def test_recent_span_must_belong_to_requested_service_in_window(self):
        now = datetime.now(timezone.utc)
        at = now - timedelta(seconds=25)
        self.assertAlmostEqual(stack_health.recent_spans(trace("checkout", at), "checkout", now), 25)
        for body in [trace("frontend", at), trace("checkout", now - timedelta(minutes=20)),
                     trace("checkout", now + timedelta(minutes=1)), b'{"error":"failed"}', b'']:
            with self.subTest(body=body), self.assertRaises(ValueError):
                stack_health.recent_spans(body, "checkout", now)

    def test_spm_requires_finite_latency_in_recent_completed_positive_bucket(self):
        now = datetime.now(timezone.utc)
        sample = now - timedelta(seconds=100)
        self.assertEqual(stack_health.check_metrics(metric(sample, 2), metric(sample, 0), now),
                         int(sample.timestamp()) // 60 * 60)
        for calls, latency, date in [(0, 0, sample), (2, 'NaN', sample), ('NaN', 1, sample),
                                     (2, 1, now), (2, 1, now - timedelta(minutes=6))]:
            with self.subTest(calls=calls, latency=latency, date=date), self.assertRaises(ValueError):
                stack_health.check_metrics(metric(date, calls), metric(date, latency), now)
        with self.assertRaises(ValueError):
            stack_health.check_metrics(metric(sample, 2), metric(sample - timedelta(minutes=1), 4), now)

    def test_latency_sample_must_match_the_calls_timestamp_within_a_minute(self):
        now = datetime(2026, 11, 19, 3, 30, tzinfo=timezone.utc)
        calls_at = now - timedelta(seconds=110)
        with self.assertRaises(ValueError):
            stack_health.check_metrics(metric(calls_at, 2), metric(calls_at + timedelta(seconds=5), 3), now)

    def test_each_stack_exercises_its_own_public_endpoints(self):
        now = datetime.now(timezone.utc)
        for stack, (base, service) in stack_health.STACKS.items():
            tracking = (stack_health.ROOT / "examples/oci/ui-config.json").read_text()
            ui = ("Jaeger UI; const JAEGER_CONFIG = " + json.dumps(json.loads(tracking)) + ";").encode()
            seen = []
            def read(url):
                seen.append(url)
                path = urlsplit(url).path
                if url == base + ("/" if stack == "oci" else "/search"):
                    return ui
                if url in stack_health.PAGES[stack]:
                    return stack_health.PAGES[stack][url][0].encode()
                if path.endswith("/api/health"):
                    return b'{"database":"ok"}'
                if path.endswith("/api/status"):
                    return b'{"status":{"overall":{"state":"green"}}}'
                if path.endswith("/api/v3/services"):
                    return json.dumps({"services": [service]}).encode()
                if path.endswith("/api/v3/traces"):
                    return trace(service, now - timedelta(seconds=20))
                if "/api/metrics/" in path:
                    return json.dumps(metric(now - timedelta(minutes=2), 1)).encode()
                self.fail(f"Unexpected URL {url}")
            with self.subTest(stack=stack):
                self.assertTrue(stack_health.check(stack, read)["uiTrackingMatched"])
                self.assertEqual(len(seen), len(set(seen)))
                ends = [parse_qs(urlsplit(u).query).get("endTs") for u in seen if "/api/metrics/" in u]
                self.assertEqual(len(ends), 2)
                self.assertIsNotNone(ends[0])
                self.assertEqual(ends[0], ends[1])
                self.assertEqual(int(ends[0][0]) % 60000, 0)
                with self.assertRaises(ValueError):
                    stack_health.check(stack, lambda _: b"unhealthy")
