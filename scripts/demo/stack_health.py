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


class TrafficPending(ValueError):
    """A valid response has no recent evidence of application traffic yet."""


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


def recent_spans(payload, service, now):
    minimum = (now - timedelta(minutes=10)).timestamp()
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
                    if minimum <= at <= now.timestamp() and span.get("traceId"):
                        latest = max(at, latest or 0)
    if latest is None:
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


def check_metrics(calls, latency, now):
    rates = samples_by_timestamp(calls, now)
    quantiles = samples_by_timestamp(latency, now)
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


def check(stack, read=fetch, *, tracking=None):
    base, service = STACKS[stack]
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
    criteria = {"query.service_name": service, "query.start_time_min": (now - timedelta(minutes=10)).isoformat(),
                "query.start_time_max": now.isoformat(), "query.search_depth": "20"}
    latest = recent_spans(read(base + "/api/v3/traces?" + urllib.parse.urlencode(criteria)), service, now)
    opts = {"service": service, "spanKind": "server", "lookback": "600000", "step": "60000", "ratePer": "600000"}
    # Both Prometheus queries must evaluate the same completed sample intervals.
    opts["endTs"] = str(int(now.replace(second=0, microsecond=0).timestamp()) * 1000)
    metrics = base + "/api/metrics/"
    calls = json.loads(read(metrics + "calls?" + urllib.parse.urlencode(opts)))
    latency = json.loads(read(metrics + "latencies?" + urllib.parse.urlencode(dict(opts, quantile="0.95"))))
    observed = check_metrics(calls, latency, now)
    return {"stack": stack, "service": service, "uiTrackingMatched": True, "publicRoutesHealthy": True,
            "newestMatchingSpanAgeSeconds": latest, "finiteSPMBucketUTC": datetime.fromtimestamp(observed, timezone.utc).isoformat()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stack", required=True, choices=STACKS)
    args = parser.parse_args()
    print(json.dumps(check(args.stack), indent=2))
