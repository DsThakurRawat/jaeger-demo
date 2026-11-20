#!/usr/bin/env python3

# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Verify the deployed application versions and a new trace through the live collector/query."""

import base64
from contextlib import ExitStack
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid


def request(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as response:
        raw = response.read(12 * 1024 * 1024 + 1)
        if len(raw) > 12 * 1024 * 1024:
            raise RuntimeError("Trace verification response exceeds the size limit")
        return json.loads(raw)


def contains_trace(value, trace_id):
    if isinstance(value, dict):
        encoded = base64.b64encode(bytes.fromhex(trace_id)).decode()
        if value.get("traceId") in (trace_id, encoded):
            return True
        return any(contains_trace(child, trace_id) for child in value.values())
    return isinstance(value, list) and any(contains_trace(child, trace_id) for child in value)


def check_trace(collector, query, attempts=30):
    trace_id = uuid.uuid4().hex
    now = time.time_ns()
    body = {"resourceSpans": [{"resource": {"attributes": [{"key": "service.name", "value": {
        "stringValue": "jaeger-demo-rollout-check"}}]}, "scopeSpans": [{"scope": {"name": "demo-deployment"},
        "spans": [{"traceId": trace_id, "spanId": uuid.uuid4().hex[:16], "name": "deployment-write-read",
                   "startTimeUnixNano": str(now), "endTimeUnixNano": str(now + 1000000)}]}]}]}
    accepted = False
    for _ in range(attempts):
        try:
            if not accepted:
                response = request(collector + "/v1/traces", body)
                if int(response.get("partialSuccess", {}).get("rejectedSpans", 0)):
                    raise RuntimeError("Collector rejected the deployment trace")
                accepted = True
            if contains_trace(request(query + "/api/v3/traces/" + trace_id), trace_id):
                print("Fresh trace successfully written and retrieved: " + trace_id)
                return
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(2)
    raise RuntimeError("Fresh deployment trace could not be written and retrieved")


def deployed_app_version(release, chart_version):
    # A chart release can repackage an unchanged application, so the chart pin
    # identifies the release and its recorded appVersion identifies the software.
    output = subprocess.run(["helm", "get", "metadata", release, "--namespace", "opensearch", "-o", "json"],
                            check=True, capture_output=True, text=True)
    metadata = json.loads(output.stdout)
    if metadata.get("status") != "deployed" or metadata.get("version") != chart_version:
        raise RuntimeError(f"Helm release opensearch/{release} is not deployed at chart pin {chart_version}")
    if not metadata.get("appVersion"):
        raise RuntimeError(f"Helm release opensearch/{release} does not record an application version")
    return metadata["appVersion"]


def verify(opensearch_chart, dashboards_chart):
    version = deployed_app_version("opensearch", opensearch_chart)
    output = subprocess.run(["kubectl", "-n", "opensearch", "exec", "opensearch-cluster-single-0", "--",
                             "curl", "-fsS", "http://localhost:9200/"], check=True, capture_output=True, text=True)
    if json.loads(output.stdout)["version"]["number"] != version:
        raise RuntimeError("Live OpenSearch version does not match the deployed chart's application version")
    version = deployed_app_version("opensearch-dashboards", dashboards_chart)
    if request("https://opensearch.demo.jaegertracing.io/api/status")["version"]["number"] != version:
        raise RuntimeError("Public Dashboards version does not match the deployed chart's application version")
    with ExitStack() as stack:
        for service, ports in (("jaeger-collector", "14318:4318"), ("jaeger-query", "16687:16686")):
            process = stack.enter_context(subprocess.Popen(
                ["kubectl", "-n", "jaeger", "port-forward", "svc/" + service, ports],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            stack.callback(process.terminate)
        check_trace("http://127.0.0.1:14318", "http://127.0.0.1:16687")


if __name__ == "__main__":
    verify(sys.argv[1], sys.argv[2])
