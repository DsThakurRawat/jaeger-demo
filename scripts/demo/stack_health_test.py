# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
import unittest
from urllib.parse import parse_qs, urlsplit

import stack_health


def metric(at, value):
    point = {"timestamp": at.isoformat(), "gaugeValue": {"doubleValue": str(value)}}
    return {"metrics": [{"metricPoints": [point]}]}


SERVER, CLIENT = 2, 3


def trace(service, at, kind=None, *more):
    """One trace of the service: a span at each (time, kind) or (time, kind, status code), as
    Jaeger's /api/v3/traces returns it. A span without an error has an empty status there."""
    spans = [{"traceId": "e1" * 16, "startTimeUnixNano": str(int(start.timestamp() * 1e9)),
              **({} if span_kind is None else {"kind": span_kind}),
              "status": {"code": code[0], "message": "failed to charge card"} if code else {}}
             for start, span_kind, *code in ((at, kind), *more)]
    batch = {"resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service}}]},
             "scopeSpans": [{"spans": spans}]}
    return json.dumps({"result": {"resourceSpans": [batch]}}).encode()


DISPATCHED = b'{"Driver":"T727756C","ETA":120000000000}'
# The requests of one run of each stack. smoke_workflow_test.py budgets the smoke check by them.
REQUESTS_PER_RUN = {"oci": 9, "otel": 10}


def public_stack(stack, spans, metrics, functional=DISPATCHED):
    """A read() for one stack: spans are the service's (time, kind) pairs, metrics answers the SPM
    queries, and functional is the body of the stack's functional request or the error it raises."""
    base, service = stack_health.STACKS[stack]
    tracking = json.loads((stack_health.ROOT / "examples/oci/ui-config.json").read_text())
    ui = ("Jaeger UI; const JAEGER_CONFIG = " + json.dumps(tracking) + ";").encode()

    def read(url):
        parts = urlsplit(url)
        if url == base + ("/" if stack == "oci" else "/search"):
            return ui
        if url in stack_health.PAGES[stack]:
            return stack_health.PAGES[stack][url][0].encode()
        if stack in stack_health.FUNCTIONAL and url == stack_health.FUNCTIONAL[stack][0]:
            if isinstance(functional, Exception):
                raise functional
            return functional
        if parts.path.endswith("/api/health"):
            return b'{"database":"ok"}'
        if parts.path.endswith("/api/status"):
            return b'{"status":{"overall":{"state":"green"}}}'
        if parts.path.endswith("/api/v3/services"):
            return json.dumps({"services": [service]}).encode()
        if parts.path.endswith("/api/v3/traces"):
            return trace(service, *spans[0], *spans[1:])
        if "/api/metrics/" in parts.path:
            return metrics(parts.path.rsplit("/", 1)[1], parse_qs(parts.query))
        raise AssertionError(f"Unexpected URL {url}")

    return read


def checkout_stack(orders, now):
    """A read() for the otel stack whose SPM is computed from completed checkout orders.

    orders holds the start times of the PlaceOrder server spans that ended. The metrics follow
    Jaeger's OpenSearch-backed SPM: one bucket per step by span start time, a calls value that
    counts the buckets of one rate window ending with its own, rounded to two decimals, and NaN
    for calls and latency alike when the bucket itself holds no span. Checkout keeps emitting
    client spans while orders hang, so the trace search always finds a fresh one of those.
    """
    starts = [order.timestamp() for order in orders]
    asked = []

    def metrics(name, query):
        end, step, rate, lookback = (int(query[key][0]) / 1000 for key in ("endTs", "step", "ratePer", "lookback"))
        asked.append(int(rate))
        points, at = [], end - lookback
        while at <= end:
            value = math.nan
            if any(at <= start < at + step for start in starts):
                counted = sum(1 for start in starts if at - rate + step <= start < at + step)
                value = round(counted / rate, 2) if name == "calls" else 12.5
            points.append({"timestamp": datetime.fromtimestamp(at, timezone.utc).isoformat(),
                           "gaugeValue": {"doubleValue": str(value)}})
            at += step
        return json.dumps({"metrics": [{"metricPoints": points}]}).encode()

    # A completed order is a server span; the fresh client span is one of a hanging order.
    spans = [(now - timedelta(seconds=5), CLIENT)] + [(order, SERVER) for order in orders]
    return public_stack("otel", spans, metrics), asked


class StackHealthTests(unittest.TestCase):
    def test_recent_span_must_belong_to_requested_service_in_window(self):
        now = datetime.now(timezone.utc)
        at = now - timedelta(seconds=25)
        self.assertAlmostEqual(stack_health.recent_spans(trace("checkout", at), "checkout", now), 25)
        for body in [trace("frontend", at), trace("checkout", now - timedelta(minutes=20)),
                     trace("checkout", now + timedelta(minutes=1)), b'{"error":"failed"}', b'']:
            with self.subTest(body=body), self.assertRaises(ValueError):
                stack_health.recent_spans(body, "checkout", now)

    def test_empty_traffic_is_distinct_from_stream_and_metrics_failures(self):
        now = datetime.now(timezone.utc)
        sample = now - timedelta(seconds=100)
        with self.assertRaises(stack_health.TrafficPending):
            stack_health.recent_spans(b"", "frontend", now)
        with self.assertRaises(stack_health.TrafficPending):
            stack_health.check_metrics(metric(sample, 0), {}, now)
        for fn, args in (
            (stack_health.recent_spans, (b'{"error":"failed"}', "frontend", now)),
            (stack_health.check_metrics, (metric(sample, 2), metric(sample, "NaN"), now)),
            (stack_health.check_metrics, (metric(sample, "NaN"), metric(sample, 2), now)),
            (stack_health.check_metrics, ({"error":"failed"}, {}, now)),
        ):
            with self.subTest(fn=fn, args=args), self.assertRaises(ValueError) as error:
                fn(*args)
            self.assertNotIsInstance(error.exception, stack_health.TrafficPending)

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
                if url == stack_health.FUNCTIONAL[stack][0]:
                    return DISPATCHED
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
                self.assertIn(stack_health.FUNCTIONAL[stack][0], seen)
                # The smoke check budgets each run by this count times the socket timeout.
                self.assertEqual(REQUESTS_PER_RUN[stack], len(seen),
                                 "Recount the budget in demo-smoke.yml and smoke_workflow_test.py")
                ends = [parse_qs(urlsplit(u).query).get("endTs") for u in seen if "/api/metrics/" in u]
                self.assertEqual(len(ends), 2)
                self.assertIsNotNone(ends[0])
                self.assertEqual(ends[0], ends[1])
                self.assertEqual(int(ends[0][0]) % 60000, 0)
                with self.assertRaises(ValueError):
                    stack_health.check(stack, lambda _: b"unhealthy")

    def test_spm_evidence_must_lie_wholly_after_a_change(self):
        utc = timezone.utc
        now = datetime(2026, 11, 25, 16, 41, 30, tzinfo=utc)
        since = datetime(2026, 11, 25, 16, 37, 10, tzinfo=utc)
        before, straddling, after = (datetime(2026, 11, 25, 16, minute, tzinfo=utc) for minute in (38, 39, 40))

        def series(values):
            points = [{"timestamp": at.isoformat(), "gaugeValue": {"doubleValue": str(value)}}
                      for at, value in values.items()]
            return {"metrics": [{"metricPoints": points}]}

        # Both samples are stamped after the change, but their two-minute windows began before it.
        stale = series({before: 2, straddling: 2})
        self.assertEqual(stack_health.check_metrics(stale, stale, now), int(straddling.timestamp()))
        with self.assertRaises(stack_health.TrafficPending):
            stack_health.check_metrics(stale, stale, now, since)
        # A wholly later sample without calls is still no evidence, whatever came before it.
        with self.assertRaises(stack_health.TrafficPending):
            stack_health.check_metrics(series({straddling: 2, after: 0}), series({straddling: 1, after: 1}), now, since)
        with self.assertRaises(ValueError):
            stack_health.check_metrics(series({straddling: 2, after: "NaN"}), series({straddling: 1}), now, since)
        with self.assertRaises(ValueError):
            stack_health.check_metrics(series({after: 2}), series({straddling: 1, after: "NaN"}), now, since)
        fresh = series({straddling: 2, after: 2})
        self.assertEqual(stack_health.check_metrics(fresh, fresh, now, since), int(after.timestamp()))
        # The first sample that can qualify is complete one step after its timestamp.
        self.assertEqual(stack_health.evidence_ready_at(since), after + timedelta(minutes=1))
        self.assertEqual(stack_health.evidence_ready_at(since.replace(second=0)), after)
        ready = stack_health.evidence_ready_at(since)
        self.assertEqual(stack_health.check_metrics(fresh, fresh, ready, since), int(after.timestamp()))

    def test_fresh_span_must_start_after_a_change(self):
        now = datetime.now(timezone.utc)
        at = now - timedelta(seconds=90)
        after, before = now - timedelta(seconds=120), now - timedelta(seconds=60)
        for kind in (SERVER, "SPAN_KIND_SERVER"):
            with self.subTest(kind=kind):
                body = trace("checkout", at, kind)
                self.assertAlmostEqual(stack_health.recent_spans(body, "checkout", now), 90)
                self.assertAlmostEqual(stack_health.recent_spans(body, "checkout", now, after), 90)
                with self.assertRaises(stack_health.TrafficPending):
                    stack_health.recent_spans(body, "checkout", now, before)
        # A routine check takes any span of the service. Bound to a change, only a server span is
        # evidence: a client span after it, beside a server span from before it, proves no request.
        for kind in (CLIENT, None, 1, "SPAN_KIND_CLIENT"):
            with self.subTest(kind=kind):
                body = trace("checkout", at, kind, (now - timedelta(seconds=300), SERVER))
                self.assertAlmostEqual(stack_health.recent_spans(body, "checkout", now), 90)
                with self.assertRaisesRegex(stack_health.TrafficPending, "server span without an error started after the change"):
                    stack_health.recent_spans(body, "checkout", now, after)
        both = trace("checkout", at, CLIENT, (now - timedelta(seconds=100), SERVER))
        self.assertAlmostEqual(stack_health.recent_spans(both, "checkout", now, after), 100)

    def test_a_failed_server_span_after_a_change_is_no_evidence(self):
        # checkout answers Internal when a charge fails, which ends its PlaceOrder span with an error.
        now = datetime.now(timezone.utc)
        after, at = now - timedelta(seconds=120), now - timedelta(seconds=90)
        for code in (2, "STATUS_CODE_ERROR"):
            with self.subTest(code=code):
                failed = trace("checkout", at, SERVER, (now - timedelta(seconds=30), SERVER, code))
                self.assertAlmostEqual(stack_health.recent_spans(failed, "checkout", now, after), 90)
                only_failed = trace("checkout", now - timedelta(seconds=300), SERVER,
                                    (at, SERVER, code), (now - timedelta(seconds=30), SERVER, code))
                # A routine check still counts the span; bound to a change it does not.
                self.assertAlmostEqual(stack_health.recent_spans(only_failed, "checkout", now), 30)
                with self.assertRaisesRegex(stack_health.TrafficPending, "without an error started after the change"):
                    stack_health.recent_spans(only_failed, "checkout", now, after)
        for code in (0, 1, "STATUS_CODE_UNSET", "STATUS_CODE_OK"):
            with self.subTest(code=code):
                ok = trace("checkout", now - timedelta(seconds=300), SERVER, (at, SERVER, code))
                self.assertAlmostEqual(stack_health.recent_spans(ok, "checkout", now, after), 90)

    def test_verification_after_a_change_fails_while_every_order_fails(self):
        # Every order since the change ended with an error: calls are positive and latency is
        # finite in buckets wholly after it, and still no order succeeded.
        now = datetime.now(timezone.utc)
        minute = now.replace(second=0, microsecond=0)
        since = minute - timedelta(minutes=4)
        attempts = [minute - timedelta(seconds=150), minute - timedelta(seconds=100), minute - timedelta(seconds=40)]

        def metrics(name, query):
            points = [{"timestamp": (minute - timedelta(minutes=n)).isoformat(),
                       "gaugeValue": {"doubleValue": "0.02" if name == "calls" else "12.5"}} for n in (1, 2)]
            return json.dumps({"metrics": [{"metricPoints": points}]}).encode()

        failing = [(now - timedelta(seconds=5), CLIENT)] + [(at, SERVER, 2) for at in attempts]
        with self.assertRaisesRegex(stack_health.TrafficPending, "checkout server span without an error"):
            stack_health.check("otel", public_stack("otel", failing, metrics), since=since)
        # The same metrics pass once one order succeeds.
        succeeded = failing + [(minute - timedelta(seconds=20), SERVER)]
        result = stack_health.check("otel", public_stack("otel", succeeded, metrics), since=since)
        self.assertEqual(result["finiteSPMBucketUTC"], (minute - timedelta(minutes=1)).isoformat())

    def test_buffered_span_metrics_do_not_pass_without_a_server_span_after_the_change(self):
        # The OCI stack's connector flushes its counters every 60 s and Prometheus scrapes them every
        # 15 s, so requests that ended before a change can raise a rate window that began after it.
        now = datetime.now(timezone.utc)
        minute = now.replace(second=0, microsecond=0)
        since = minute - timedelta(minutes=3, seconds=10)

        def metrics(name, query):
            self.assertEqual(query["ratePer"], [str(stack_health.FRESH_RATE_SECONDS * 1000)])
            # Positive in the one bucket whose window lies wholly after the change, from the flush.
            return json.dumps(metric(minute - timedelta(minutes=1), 0.5 if name == "calls" else 20)).encode()

        # frontend's own client calls continue; its last server span ended just before the change.
        stale = [(now - timedelta(seconds=10), CLIENT), (since - timedelta(seconds=20), SERVER)]
        with self.assertRaisesRegex(stack_health.TrafficPending, "frontend server span without an error started after the change"):
            stack_health.check("oci", public_stack("oci", stale, metrics), since=since)
        fresh = stale + [(now - timedelta(seconds=30), SERVER)]
        result = stack_health.check("oci", public_stack("oci", fresh, metrics), since=since)
        self.assertEqual(result["evidenceSinceUTC"], since.isoformat())
        self.assertAlmostEqual(result["newestMatchingSpanAgeSeconds"], 30, delta=1)

    def test_a_check_right_after_a_change_needs_a_bucket_newer_than_it(self):
        # Issue #160: orders completed until the change and none since. Checked at once, the
        # routine check passes on the buckets from before the change; bound to the change it waits.
        now = datetime.now(timezone.utc)
        since = now - timedelta(seconds=20)
        read, asked = checkout_stack([since - timedelta(seconds=20 * n) for n in range(1, 45)], now)
        self.assertIsNone(stack_health.check("otel", read)["evidenceSinceUTC"])
        with self.assertRaisesRegex(stack_health.TrafficPending, "checkout server span without an error started after the change"):
            stack_health.check("otel", read, since=since)
        # Ten-minute rates for the routine check. The bound one stopped at the missing server span.
        self.assertEqual(asked, [600, 600])
        # An order that hangs leaves no server span, but one that completes right after the change
        # does: then the check waits for the bucket instead.
        read, asked = checkout_stack([now - timedelta(seconds=10)], now)
        with self.assertRaisesRegex(stack_health.TrafficPending, "wholly after the change"):
            stack_health.check("otel", read, since=since)
        self.assertEqual(asked, [120, 120])

    def test_verification_after_a_change_fails_until_checkout_completes_orders_again(self):
        # The acceptance of issue #160. The change was four minutes before the current minute, so
        # two completed buckets lie wholly after it.
        now = datetime.now(timezone.utc)
        minute = now.replace(second=0, microsecond=0)
        since = minute - timedelta(minutes=4)
        until_change = [since - timedelta(seconds=20 * n) for n in range(1, 45)]
        stalled, _ = checkout_stack(until_change, now)
        with self.assertRaisesRegex(stack_health.TrafficPending, "checkout server span without an error started after the change"):
            stack_health.check("otel", stalled, since=since)
        resumed = [minute - timedelta(seconds=150), minute - timedelta(seconds=100), minute - timedelta(seconds=40)]
        recovered, _ = checkout_stack(until_change + resumed, now)
        result = stack_health.check("otel", recovered, since=since)
        self.assertEqual(result["evidenceSinceUTC"], since.isoformat())
        bucket = datetime.fromisoformat(result["finiteSPMBucketUTC"])
        self.assertGreaterEqual(bucket, since + timedelta(seconds=stack_health.FRESH_RATE_SECONDS))
        self.assertEqual(bucket, minute - timedelta(minutes=1))

    def test_since_must_be_a_past_time_with_an_offset(self):
        now = datetime.now(timezone.utc)
        read, asked = checkout_stack([], now)
        for since in (now + timedelta(minutes=1), datetime.now()):
            with self.subTest(since=since), self.assertRaisesRegex(ValueError, "since must be"):
                stack_health.check("otel", read, since=since)
        self.assertEqual(asked, [])
        self.assertEqual(stack_health.parse_time("2026-11-25T16:37:00Z"),
                         datetime(2026, 11, 25, 16, 37, tzinfo=timezone.utc))
        with self.assertRaises(argparse.ArgumentTypeError):
            stack_health.parse_time("2026-11-25T16:37:00")

    def test_oci_verification_after_a_change_needs_a_completed_dispatch(self):
        # The kubelet probes HotROD at /hotrod, and each probe is a frontend server span. With
        # dispatches hanging, the probes alone keep a fresh server span without an error and a
        # positive, finite SPM bucket wholly after the change.
        now = datetime.now(timezone.utc)
        minute = now.replace(second=0, microsecond=0)
        since = minute - timedelta(minutes=4)
        probes = [(now - timedelta(seconds=5), CLIENT)] + [(now - timedelta(seconds=10 * n), SERVER) for n in range(1, 20)]

        def metrics(name, query):
            return json.dumps(metric(minute - timedelta(minutes=1), 0.1 if name == "calls" else 3)).encode()

        for hanging in (TimeoutError("timed out"), b'{"error":"redis timeout"}', b""):
            with self.subTest(hanging=hanging), self.assertRaises((ValueError, OSError)):
                stack_health.check("oci", public_stack("oci", probes, metrics, hanging), since=since)
        result = stack_health.check("oci", public_stack("oci", probes, metrics), since=since)
        self.assertTrue(result["functionalRequestCompleted"])
        values = (stack_health.ROOT / "examples/oci/jaeger-values.yaml").read_text()
        self.assertIn("--basepath=/hotrod", values)
        self.assertTrue(stack_health.FUNCTIONAL["oci"][0].startswith("https://demo.jaegertracing.io/hotrod/dispatch?"))

    def test_the_routine_check_needs_a_completed_dispatch(self):
        # The smoke check runs the routine check. On the OCI stack the kubelet's probes of
        # /hotrod alone give it a fresh frontend span and a positive, finite SPM bucket, and the
        # HotROD page answers, so without a dispatch of its own it passes while dispatches hang.
        now = datetime.now(timezone.utc)
        minute = now.replace(second=0, microsecond=0)
        probes = [(now - timedelta(seconds=10 * n), SERVER) for n in range(1, 20)]

        def metrics(name, query):
            return json.dumps(metric(minute - timedelta(minutes=2), 0.1 if name == "calls" else 3)).encode()

        self.assertEqual(["oci", "otel"], sorted(stack_health.FUNCTIONAL))
        for stack in stack_health.STACKS:
            for hanging in (TimeoutError("timed out"), b'{"error":"redis timeout"}', b""):
                with self.subTest(stack=stack, hanging=hanging), self.assertRaises((ValueError, OSError)):
                    stack_health.check(stack, public_stack(stack, probes, metrics, hanging))
            with self.subTest(stack=stack):
                result = stack_health.check(stack, public_stack(stack, probes, metrics))
                self.assertTrue(result["functionalRequestCompleted"])
                self.assertIsNone(result["evidenceSinceUTC"])
        # The OTel stack's HotROD has no base path, so its dispatch is at the root of its host.
        values = (stack_health.ROOT / "examples/otel-demo/jaeger-values.yaml").read_text()
        self.assertNotIn("--basepath", values)
        self.assertTrue(stack_health.FUNCTIONAL["otel"][0].startswith("https://hotrod.demo.jaegertracing.io/dispatch?"))
        self.assertIn("https://hotrod.demo.jaegertracing.io/", stack_health.PAGES["otel"])

    def test_evidence_after_a_change_comes_from_the_newest_traces_only(self):
        # Issue #160 developed this way: orders completed for some minutes after the change, then
        # every later one hung. Jaeger returns the newest traces first, and the search depth is
        # kept, so an early success behind that many newer hanging orders does not pass.
        now = datetime.now(timezone.utc)
        minute = now.replace(second=0, microsecond=0)
        since = minute - timedelta(minutes=8)
        base, service = stack_health.STACKS["otel"]
        early = [(since + timedelta(seconds=10), SERVER)]
        hanging = [(now - timedelta(seconds=10 * n), CLIENT) for n in range(1, stack_health.SEARCH_DEPTH + 1)]
        depths = []

        def metrics(name, query):
            return json.dumps(metric(minute - timedelta(minutes=1), 0.02 if name == "calls" else 12.5)).encode()

        def newest_first(orders):
            inner = public_stack("otel", orders, metrics)

            def read(url):
                parts = urlsplit(url)
                if not parts.path.endswith("/api/v3/traces"):
                    return inner(url)
                depth = int(parse_qs(parts.query)["query.search_depth"][0])
                depths.append(depth)
                # One trace per order, the newest first, cut at the requested depth.
                kept = sorted(orders, key=lambda order: order[0], reverse=True)[:depth]
                return b"\n".join(trace(service, *order) for order in kept)
            return read

        with self.assertRaisesRegex(stack_health.TrafficPending, "checkout server span without an error"):
            stack_health.check("otel", newest_first(early + hanging), since=since)
        self.assertEqual([stack_health.SEARCH_DEPTH], depths)
        # With one hanging order fewer, the early success is among the newest traces and counts.
        self.assertEqual(stack_health.check("otel", newest_first(early + hanging[1:]), since=since)["evidenceSinceUTC"],
                         since.isoformat())

