# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Loopback admission fixture for the exact two approved storage chart states."""

from base64 import b64encode
from contextlib import contextmanager
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ssl
import threading


MARKER = "scheduled-writer-rehearsal"
DENIED = "Only the two reviewed storage chart states are authorized"


def desired_content(obj):
    result = deepcopy(obj)
    result.pop("status", None)
    for key in ["resourceVersion", "managedFields", "generation", "creationTimestamp"]:
        result["metadata"].pop(key, None)
    return result


def states(baseline):
    allowed = {}
    for resource in baseline:
        obj = desired_content(resource)
        key = (obj["kind"], obj["metadata"]["name"])
        candidate = deepcopy(obj)
        if key in {("Service", "opensearch-cluster-single"), ("Service", "opensearch-dashboards")}:
            candidate["metadata"].setdefault("annotations", {})[MARKER] = "verified"
        if obj["kind"] in ("StatefulSet", "Deployment"):
            candidate["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[MARKER] = "verified"
        allowed[key] = (obj, candidate)
    return allowed


@contextmanager
def enforce(root, baseline, admin, writer):
    allowed = states(baseline)
    decisions = {"allowed": 0, "denied": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            review = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            request = review["request"]
            key = (request["kind"]["kind"], request["name"])
            permitted = (request["userInfo"]["username"] == writer and request["namespace"] == "opensearch"
                         and request["operation"] == "UPDATE" and key in allowed
                         and all(desired_content(request[field]) in allowed[key] for field in ["oldObject", "object"]))
            decisions["allowed" if permitted else "denied"] += 1
            response = {"uid": request["uid"], "allowed": permitted}
            if not permitted:
                response["status"] = {"message": DENIED, "code": 403}
            body = json.dumps({"apiVersion": "admission.k8s.io/v1", "kind": "AdmissionReview",
                               "response": response}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(root / "tls.pem", root / "tls.key")
        server.socket = tls.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            admin({"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingWebhookConfiguration",
                   "metadata": {"name": "fixture-storage-chart-contents"}, "webhooks": [{
                       "name": "storage.fixture.invalid", "admissionReviewVersions": ["v1"], "sideEffects": "None",
                       "timeoutSeconds": 4, "failurePolicy": "Fail",
                       "clientConfig": {"url": f"https://127.0.0.1:{server.server_port}/storage",
                                        "caBundle": b64encode((root / "tls.pem").read_bytes()).decode()},
                       "matchConditions": [{"name": "fixture-user", "expression":
                           "request.userInfo.username == '" + writer + "' && request.namespace == 'opensearch'"}],
                       "rules": [{"apiGroups": ["", "policy", "apps", "rbac.authorization.k8s.io"],
                                  "apiVersions": ["v1"], "operations": ["CREATE", "UPDATE", "DELETE"],
                                  "resources": ["services", "configmaps", "serviceaccounts", "poddisruptionbudgets",
                                                "statefulsets", "deployments", "rolebindings"]}],
                   }]})
            yield decisions
        finally:
            server.shutdown()
            thread.join()
