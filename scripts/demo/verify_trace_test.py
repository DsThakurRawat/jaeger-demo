# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import base64
import http.client
import io
import json
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

import verify_trace


RELEASES = {"opensearch": ("3.9.0", "3.9.0"), "opensearch-dashboards": ("3.9.0", "3.9.0")}


def commands(live="3.9.0", releases=RELEASES, status="deployed"):
    """Answer Helm release metadata and the in-pod OpenSearch version request."""
    def run(args, **kwargs):
        if args[:3] == ["helm", "get", "metadata"]:
            chart, app = releases[args[3]]
            return MagicMock(stdout=json.dumps({"name": args[3], "version": chart, "appVersion": app, "status": status}))
        return MagicMock(stdout=json.dumps({"version": {"number": live}}))
    return run


def http_error(code, reason):
    return urllib.error.HTTPError("https://opensearch.demo.jaegertracing.io/api/status", code, reason, {}, io.BytesIO())


def http_ok(body):
    response = MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps(body).encode()
    return response


class TraceVerificationTests(unittest.TestCase):
    def test_finds_hex_and_base64_trace_ids_in_streaming_responses(self):
        trace_id = "ab" * 16
        for value in (trace_id, base64.b64encode(bytes.fromhex(trace_id)).decode()):
            self.assertTrue(verify_trace.contains_trace({"result": [{"traceId": value}]}, trace_id))
        self.assertFalse(verify_trace.contains_trace({"traceId": "other"}, trace_id))
        self.assertFalse(verify_trace.contains_trace(None, trace_id))

    @patch("verify_trace.urllib.request.urlopen")
    def test_json_transport(self, urlopen):
        urlopen.return_value.__enter__.return_value.read.return_value = b'{"ok":true}'
        self.assertEqual({"ok": True}, verify_trace.request("http://test", {"span": 1}))
        self.assertEqual({"span": 1}, json.loads(urlopen.call_args.args[0].data))

    @patch("verify_trace.time.sleep")
    @patch("verify_trace.request")
    def test_writes_once_then_waits_for_exact_trace(self, request, sleep):
        trace_id = "12" * 16
        request.side_effect = [urllib.error.URLError("not ready"), {}, {"result": []}, {"result": [{"traceId": trace_id}]}]
        with patch("verify_trace.uuid.uuid4") as uuid:
            uuid.return_value.hex = trace_id
            verify_trace.check_trace("collector", "query", attempts=3)
        self.assertEqual(4, request.call_count)
        span = request.call_args_list[1].args[1]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        self.assertEqual(trace_id, span["traceId"])
        self.assertEqual(2, sleep.call_count)

    @patch("verify_trace.time.sleep")
    @patch("verify_trace.request")
    def test_missing_trace_and_rejected_ingestion_fail(self, request, sleep):
        request.return_value = {}
        with self.assertRaises(RuntimeError):
            verify_trace.check_trace("collector", "query", attempts=1)
        request.return_value = {"partialSuccess": {"rejectedSpans": "1"}}
        with self.assertRaisesRegex(RuntimeError, "rejected"):
            verify_trace.check_trace("collector", "query", attempts=1)

    @patch("verify_trace.subprocess.run")
    @patch("verify_trace.request")
    def test_rejects_wrong_server_or_dashboard_version(self, request, run):
        run.side_effect = commands(live="3.7.0")
        with self.assertRaisesRegex(RuntimeError, "OpenSearch"):
            verify_trace.verify("3.9.0", "3.9.0")
        run.side_effect = commands(live="3.9.0")
        request.return_value = {"version": {"number": "3.7.0"}}
        with self.assertRaisesRegex(RuntimeError, "Dashboards"):
            verify_trace.verify("3.9.0", "3.9.0")

    @patch("verify_trace.check_trace")
    @patch("verify_trace.subprocess.Popen")
    @patch("verify_trace.subprocess.run")
    @patch("verify_trace.time.sleep")
    @patch("verify_trace.urllib.request.urlopen")
    def test_waits_for_a_dashboards_route_that_answers_503_after_the_rollout(self, urlopen, sleep, run, popen, check):
        # The public route answered 503 for a short time after Helm reported the Deployment ready (#111).
        run.side_effect = commands(live="3.9.0")
        urlopen.side_effect = [http_error(503, "Service Unavailable"), http_ok({"version": {"number": "3.9.0"}})]
        verify_trace.verify("3.9.0", "3.9.0")
        self.assertEqual(["https://opensearch.demo.jaegertracing.io/api/status"] * 2,
                         [c.args[0].full_url for c in urlopen.call_args_list])
        sleep.assert_called_once_with(2)
        check.assert_called_once()
        # The version is still compared once the route answers.
        urlopen.side_effect = [http_error(503, "Service Unavailable"), http_ok({"version": {"number": "3.7.0"}})]
        with self.assertRaisesRegex(RuntimeError, "Dashboards version does not match"):
            verify_trace.verify("3.9.0", "3.9.0")
        check.assert_called_once()

    @patch("verify_trace.check_trace")
    @patch("verify_trace.subprocess.Popen")
    @patch("verify_trace.subprocess.run")
    @patch("verify_trace.time.sleep")
    @patch("verify_trace.urllib.request.urlopen")
    def test_fails_with_the_last_status_when_dashboards_stays_unavailable(self, urlopen, sleep, run, popen, check):
        run.side_effect = commands(live="3.9.0")
        urlopen.side_effect = lambda *args, **kwargs: (_ for _ in ()).throw(http_error(503, "Service Unavailable"))
        with self.assertRaisesRegex(RuntimeError, r"unavailable after 30 attempts; last result: HTTP 503 Service Unavailable$"):
            verify_trace.verify("3.9.0", "3.9.0")
        self.assertEqual(30, urlopen.call_count)
        self.assertEqual(30, sleep.call_count)
        check.assert_not_called()
        popen.assert_not_called()

    @patch("verify_trace.time.sleep")
    @patch("verify_trace.request")
    def test_dashboards_status_retry_is_bounded_and_limited_to_unavailable_routes(self, request, sleep):
        # Gateway statuses and connection failures are retried, and the failure names the last one.
        request.side_effect = [http_error(502, "Bad Gateway"), urllib.error.URLError("connection refused"),
                               http.client.RemoteDisconnected("closed"), TimeoutError("timed out"),
                               http_error(504, "Gateway Timeout")]
        with self.assertRaisesRegex(RuntimeError, r"after 5 attempts; last result: HTTP 504 Gateway Timeout$"):
            verify_trace.dashboards_status("https://dashboards.test/api/status", attempts=5)
        self.assertEqual(5, request.call_count)
        self.assertEqual(5, sleep.call_count)
        request.side_effect = [http_error(503, "Service Unavailable"), ConnectionResetError("reset")]
        with self.assertRaisesRegex(RuntimeError, r"after 2 attempts; last result: ConnectionResetError: reset$"):
            verify_trace.dashboards_status("https://dashboards.test/api/status", attempts=2)
        request.side_effect = [urllib.error.URLError("connection refused"), {"version": {"number": "3.9.0"}}]
        self.assertEqual({"version": {"number": "3.9.0"}},
                         verify_trace.dashboards_status("https://dashboards.test/api/status", attempts=2))
        # Any other HTTP status is an answer from the route, and so is an invalid body.
        for code, reason in ((401, "Unauthorized"), (404, "Not Found"), (500, "Internal Server Error")):
            with self.subTest(code=code):
                request.reset_mock()
                request.side_effect = [http_error(code, reason), {"version": {"number": "3.9.0"}}]
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    verify_trace.dashboards_status("https://dashboards.test/api/status", attempts=2)
                self.assertEqual(code, raised.exception.code)
                self.assertEqual(1, request.call_count)
        request.reset_mock()
        request.side_effect = [ValueError("not JSON"), {"version": {"number": "3.9.0"}}]
        with self.assertRaisesRegex(ValueError, "not JSON"):
            verify_trace.dashboards_status("https://dashboards.test/api/status", attempts=2)
        self.assertEqual(1, request.call_count)

    @patch("verify_trace.check_trace")
    @patch("verify_trace.subprocess.Popen")
    @patch("verify_trace.subprocess.run")
    @patch("verify_trace.request")
    def test_compares_live_versions_with_each_release_application_version(self, request, run, popen, check):
        # Chart-only releases keep the application version, so each chart pin can differ from its appVersion.
        # Every version here is distinct, so swapped or reused arguments cannot pass.
        releases = {"opensearch": ("3.2.1", "3.2.0"), "opensearch-dashboards": ("3.2.3", "3.2.2")}
        run.side_effect = commands(live="3.2.0", releases=releases)
        request.return_value = {"version": {"number": "3.2.2"}}
        verify_trace.verify("3.2.1", "3.2.3")
        check.assert_called_once()
        self.assertEqual([["helm", "get", "metadata", "opensearch", "--namespace", "opensearch", "-o", "json"],
                          ["helm", "get", "metadata", "opensearch-dashboards", "--namespace", "opensearch", "-o", "json"]],
                         [c.args[0] for c in run.call_args_list if c.args[0][0] == "helm"])
        rejected = {
            # The chart pins themselves are not accepted as live application versions.
            "OpenSearch chart pin": ("3.2.1", "3.2.2", "OpenSearch"),
            "Dashboards chart pin": ("3.2.0", "3.2.3", "Dashboards"),
            # Neither release is checked against the other release's application version.
            "Dashboards appVersion on OpenSearch": ("3.2.2", "3.2.2", "OpenSearch"),
            "OpenSearch appVersion on Dashboards": ("3.2.0", "3.2.0", "Dashboards"),
        }
        for case, (live, dashboards, message) in rejected.items():
            with self.subTest(case=case):
                run.side_effect = commands(live=live, releases=releases)
                request.return_value = {"version": {"number": dashboards}}
                with self.assertRaisesRegex(RuntimeError, message):
                    verify_trace.verify("3.2.1", "3.2.3")
        # Pins passed in the wrong order do not match the deployed releases.
        with self.assertRaisesRegex(RuntimeError, "is not deployed"):
            run.side_effect = commands(live="3.2.0", releases=releases)
            verify_trace.verify("3.2.3", "3.2.1")
        check.assert_called_once()

    @patch("verify_trace.check_trace")
    @patch("verify_trace.subprocess.Popen")
    @patch("verify_trace.subprocess.run")
    @patch("verify_trace.request")
    def test_rejects_releases_that_are_not_deployed_at_their_chart_pins(self, request, run, popen, check):
        request.return_value = {"version": {"number": "3.9.0"}}
        cases = {
            "OpenSearch chart": (commands(), ("3.9.1", "3.9.0"), "opensearch/opensearch is not deployed"),
            "Dashboards chart": (commands(), ("3.9.0", "3.9.1"), "opensearch/opensearch-dashboards is not deployed"),
            "failed release": (commands(status="failed"), ("3.9.0", "3.9.0"), "is not deployed"),
            "pending release": (commands(status="pending-upgrade"), ("3.9.0", "3.9.0"), "is not deployed"),
            "no application version": (commands(releases={"opensearch": ("3.9.0", "")}), ("3.9.0", "3.9.0"),
                                       "application version"),
        }
        for case, (command, pins, message) in cases.items():
            with self.subTest(case=case):
                run.side_effect = command
                with self.assertRaisesRegex(RuntimeError, message):
                    verify_trace.verify(*pins)
        check.assert_not_called()
        popen.assert_not_called()

    @patch("verify_trace.check_trace")
    @patch("verify_trace.subprocess.Popen")
    @patch("verify_trace.subprocess.run")
    @patch("verify_trace.request")
    def test_cleans_up_port_forwards_even_when_trace_verification_fails(self, request, run, popen, check):
        run.side_effect = commands(live="3.9.0")
        request.return_value = {"version": {"number": "3.9.0"}}
        first, second = MagicMock(), MagicMock()
        popen.side_effect = [first, second]
        check.side_effect = RuntimeError("trace failed")
        with self.assertRaisesRegex(RuntimeError, "trace failed"):
            verify_trace.verify("3.9.0", "3.9.0")
        first.__enter__.return_value.terminate.assert_called_once()
        second.__enter__.return_value.terminate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
