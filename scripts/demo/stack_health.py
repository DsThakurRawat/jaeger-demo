#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Check public hosted-demo routes, recent application traffic and span metrics."""

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[2]
STACKS = {
    "oci": ("https://demo.jaegertracing.io/jaeger", "frontend"),
    "otel": ("https://jaeger.demo.jaegertracing.io", "checkout"),
}
PAGES = {
    "oci": {"https://demo.jaegertracing.io/hotrod": ("HotROD", "Rides On Demand"),
            "https://demo.jaegertracing.io/grafana/": ("Grafana",)},
    "otel": {"https://hotrod.demo.jaegertracing.io/": ("HotROD", "Rides On Demand"),
             "https://shop.demo.jaegertracing.io/": ("Astronomy Shop", "OpenTelemetry"),
             "https://opensearch.demo.jaegertracing.io/": ("OpenSearch Dashboards",)},
}


# Evidence newer than a change. A calls value is a rate over a window that ends at its
# sample, so a sample stamped after a change still counts spans from before it until the
# whole window lies after the change. The routine ten-minute window would delay that proof
# by ten minutes, so a check bound to a change asks for two-minute rates instead: the
# shortest window that always spans a flush of the span metrics the OCI stack scrapes, and
# two one-minute buckets of the spans the OTel stack aggregates.
#
# A bucket alone is not that proof on the OCI stack. Its span metrics connector flushes
# every 60 seconds and Prometheus scrapes it every 15, so a window that began after the
# change can still count calls that ended up to about 75 seconds before it began. The
# proof that a request completed after the change is therefore a server span of the
# service that started after it: a span reaches storage only once it has ended. A server
# span with an error status ended too, but its request failed, so it is not that proof:
# checkout answers Internal when a charge fails.
#
# On the OCI stack even that span can be a probe. The kubelet's liveness and readiness
# probes request HotROD's /hotrod (examples/oci/jaeger-values.yaml), and each is a frontend
# server span, so the spans and the span metrics stay healthy while dispatches hang. A check
# bound to a change therefore completes one dispatch itself, as the deploy scripts did.
FUNCTIONAL = {"oci": ("https://demo.jaegertracing.io/hotrod/dispatch?customer=123", '"Driver"')}
ROUTINE_RATE_SECONDS = 600
FRESH_RATE_SECONDS = 120
STEP_SECONDS = 60


class TrafficPending(ValueError):
    """A valid response has no recent evidence of application traffic yet."""


def evidence_ready_at(since):
    """The earliest time a completed SPM sample can lie wholly after since."""
    first = math.ceil((since.timestamp() + FRESH_RATE_SECONDS) / STEP_SECONDS) * STEP_SECONDS
    return datetime.fromtimestamp(first + STEP_SECONDS, timezone.utc)


def fetch(url, *, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "Jaeger-demo-release-verification"})
    with urllib.request.urlopen(req, timeout=timeout) as conn:
        source, target = urllib.parse.urlsplit(url), urllib.parse.urlsplit(conn.url)
        if conn.status != 200 or target.scheme != "https" or target.netloc != source.netloc:
            raise ValueError(f"Unexpected HTTP response or destination for {source.hostname}{source.path}")
        raw = conn.read(12 * 1024 * 1024 + 1)
        if len(raw) > 12 * 1024 * 1024:
            raise ValueError("Public health response exceeds size limit")
        return raw


SERVER_KINDS = (2, "SPAN_KIND_SERVER")
ERROR_STATUSES = (2, "STATUS_CODE_ERROR")


def recent_spans(payload, service, now, since=None):
    """The age of the newest span of the service. With since, of its newest server span after it without an error."""
    minimum = (now - timedelta(minutes=10)).timestamp()
    if since is not None:
        minimum = max(minimum, since.timestamp())
    latest = None
    for line in payload.splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if "error" in entry:
            raise ValueError("Jaeger search returned a stream error")
        for batch in entry.get("result", {}).get("resourceSpans", []):
            attrs = batch.get("resource", {}).get("attributes", [])
            if not any(a.get("key") == "service.name" and a.get("value", {}).get("stringValue") == service for a in attrs):
                continue
            for scope in batch.get("scopeSpans", []):
                for span in scope.get("spans", []):
                    at = int(span["startTimeUnixNano"]) / 1e9
                    if since is not None and (span.get("kind") not in SERVER_KINDS
                                              or (span.get("status") or {}).get("code") in ERROR_STATUSES):
                        # Other spans of the service still arrive while its requests hang, and a
                        # server span with an error status is a request that failed.
                        continue
                    if minimum <= at <= now.timestamp() and span.get("traceId"):
                        latest = max(at, latest or 0)
    if latest is None:
        if since is not None:
            raise TrafficPending(f"No {service} server span without an error started after the change")
        raise TrafficPending(f"No recent matching spans for {service}")
    return round(now.timestamp() - latest, 3)


def samples_by_timestamp(payload, now):
    if "error" in payload:
        raise ValueError("Jaeger metrics returned an error")
    values = {}
    for series in payload.get("metrics", []):
        for point in series.get("metricPoints", []):
            sample = datetime.fromisoformat(point["timestamp"].replace("Z", "+00:00"))
            age = (now - sample).total_seconds()
            if 60 <= age <= 240:
                value = float(point["gaugeValue"]["doubleValue"])
                values.setdefault(sample, []).append(value)
    return values


def check_metrics(calls, latency, now, since=None):
    rates = samples_by_timestamp(calls, now)
    quantiles = samples_by_timestamp(latency, now)
    if since is not None:
        # Only a sample whose whole rate window began at or after the change is evidence of it.
        first = since + timedelta(seconds=FRESH_RATE_SECONDS)
        rates = {timestamp: values for timestamp, values in rates.items() if timestamp >= first}
        if not rates:
            raise TrafficPending("No completed SPM bucket lies wholly after the change yet")
    # Evaluate latency only in a completed bucket with observed traffic.
    for timestamp in sorted(rates, reverse=True):
        if all(math.isfinite(v) and v >= 0 for v in rates[timestamp]) and sum(rates[timestamp]) > 0:
            samples = quantiles.get(timestamp, [])
            if samples and all(math.isfinite(v) and v >= 0 for v in samples):
                return int(timestamp.timestamp()) // 60 * 60
            raise ValueError("Non-finite or missing SPM latency during observed traffic")
    if any(not math.isfinite(v) or v < 0 for bucket in rates.values() for v in bucket):
        raise ValueError("Invalid SPM call rate")
    raise TrafficPending("No recent completed SPM bucket with positive calls")


def check(stack, read=fetch, *, tracking=None, since=None):
    """Check one stack. With since, a server span without an error and the SPM bucket must be newer than
    that time, and the stack's functional request, if it has one, must complete."""
    base, service = STACKS[stack]
    if since is not None and (since.tzinfo is None or since > datetime.now(timezone.utc)):
        raise ValueError("since must be a past time with a UTC offset")
    ui = read(base + ("/" if stack == "oci" else "/search")).decode()
    if tracking is None:
        tracking = json.loads((ROOT / "examples/oci/ui-config.json").read_text())["tracking"]
    match = re.search(r"const JAEGER_CONFIG = (.*?);", ui)
    if "Jaeger UI" not in ui or not match or json.loads(match[1]).get("tracking") != tracking:
        raise ValueError(f"{stack} Jaeger UI or its tracking configuration differs")
    for url, markers in PAGES[stack].items():
        body = read(url).decode()
        if not any(m.lower() in body.lower() for m in markers):
            raise ValueError(f"Unexpected content at {url}")
    functional = FUNCTIONAL.get(stack) if since is not None else None
    if functional and functional[1] not in read(functional[0]).decode():
        raise ValueError(f"The {stack} functional request did not complete")
    if stack == "oci":
        if json.loads(read("https://demo.jaegertracing.io/grafana/api/health")).get("database") != "ok":
            raise ValueError("Grafana database is unhealthy")
    else:
        dashboards = json.loads(read("https://opensearch.demo.jaegertracing.io/api/status"))
        if dashboards["status"]["overall"]["state"] != "green":
            raise ValueError("OpenSearch Dashboards is unhealthy")
    catalog = json.loads(read(base + "/api/v3/services"))
    services = catalog.get("services", [])
    if "error" in catalog or not isinstance(services, list) or not all(isinstance(s, str) for s in services):
        raise ValueError("Invalid Jaeger service catalog")
    if service not in services:
        raise TrafficPending(f"{service} is missing from Jaeger")
    now = datetime.now(timezone.utc)
    earliest = now - timedelta(minutes=10)
    if since is not None:
        earliest = max(earliest, since)
    criteria = {"query.service_name": service, "query.start_time_min": earliest.isoformat(),
                "query.start_time_max": now.isoformat(), "query.search_depth": "20"}
    latest = recent_spans(read(base + "/api/v3/traces?" + urllib.parse.urlencode(criteria)), service, now, since)
    rate = ROUTINE_RATE_SECONDS if since is None else FRESH_RATE_SECONDS
    opts = {"service": service, "spanKind": "server", "lookback": "600000", "step": str(STEP_SECONDS * 1000),
            "ratePer": str(rate * 1000)}
    # Both Prometheus queries must evaluate the same completed sample intervals.
    opts["endTs"] = str(int(now.replace(second=0, microsecond=0).timestamp()) * 1000)
    metrics = base + "/api/metrics/"
    calls = json.loads(read(metrics + "calls?" + urllib.parse.urlencode(opts)))
    latency = json.loads(read(metrics + "latencies?" + urllib.parse.urlencode(dict(opts, quantile="0.95"))))
    observed = check_metrics(calls, latency, now, since)
    return {"stack": stack, "service": service, "uiTrackingMatched": True, "publicRoutesHealthy": True,
            "newestMatchingSpanAgeSeconds": latest, "finiteSPMBucketUTC": datetime.fromtimestamp(observed, timezone.utc).isoformat(),
            "evidenceSinceUTC": since.isoformat() if since is not None else None,
            "functionalRequestCompleted": True if functional else None}


def parse_time(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("--since needs a UTC offset, for example 2026-11-25T16:37:00Z")
    return parsed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", required=True, choices=STACKS)
    parser.add_argument("--since", type=parse_time, metavar="TIME",
                        help="ISO 8601 time of a change, for example 2026-11-25T16:37:00Z. A server span and "
                             "the SPM bucket must then be newer than it; the first such bucket is complete three "
                             "to four minutes after it.")
    args = parser.parse_args()
    print(json.dumps(check(args.stack, since=args.since), indent=2))
