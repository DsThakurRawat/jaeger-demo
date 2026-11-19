# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Two harmless fixture approval partitions used only by the disposable rehearsals."""

import hashlib
import json

PARTS = ("fixture-approved-charts", "fixture-approved-inputs")


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class FixtureApprovals:
    def __init__(self, store, paths, after_part=lambda part: None):
        self.store, self.paths = store, paths
        self.after_part = after_part

    def rows(self):
        return {p: self.store.read(("jaeger-demo-operations", p))["data"] for p in PARTS}

    def publish_approvals(self, files, slot, receipt):
        expected = {}
        for index, part in enumerate(PARTS):
            members = {
                name: hashlib.sha256(value).hexdigest()
                for name, value in files.items()
                if name.startswith("charts/") == (index == 0)
            }
            if not members:
                raise ValueError("Missing fixture approval partition")
            expected[part] = {**receipt, "slot": slot, "partitionSHA": digest(members)}
            ref = ("jaeger-demo-operations", part)
            old = self.store.read(ref)
            self.store.patch(ref, old, [{"op": "replace", "path": "/data", "value": expected[part]}])
            self.after_part(part)
        if self.rows() != expected:
            raise ValueError("Partial fixture approval publication")
        return digest(expected)

    def verify_approvals(self, slot, receipt):
        import scheduled_bundle

        wanted = scheduled_bundle.approval_commitment(
            self.paths.store, slot, receipt, verification_key=self.paths.keys / "verification-key.pem"
        )
        actual = self.rows()
        return (
            all(
                row.get("slot") == slot and all(row.get(k) == v for k, v in receipt.items())
                for row in actual.values()
            )
            and digest(actual) == wanted
        )


def fixture():
    import scheduled_receiver

    return FixtureApprovals(scheduled_receiver.InClusterControl(), scheduled_receiver.Paths())


def publish_approvals(files, slot, receipt):
    return fixture().publish_approvals(files, slot, receipt)


def verify_approvals(slot, receipt):
    return fixture().verify_approvals(slot, receipt)


def resources(namespace, request_name, preparer):
    api = "admissionregistration.k8s.io/v1"
    role = "rbac.authorization.k8s.io"
    for part in PARTS:
        yield {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": part, "namespace": namespace},
            "data": {"slot": "seed", "configurationSHA": "", "bundleSHA256": "", "partitionSHA": ""},
        }
        yield {
            "apiVersion": api,
            "kind": "ValidatingAdmissionPolicy",
            "metadata": {"name": part},
            "spec": {
                "failurePolicy": "Fail",
                "paramKind": {"apiVersion": "v1", "kind": "ConfigMap"},
                "matchConstraints": {
                    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}},
                    "resourceRules": [
                        {
                            "apiGroups": [""],
                            "apiVersions": ["v1"],
                            "operations": ["UPDATE"],
                            "resources": ["configmaps"],
                            "resourceNames": [request_name],
                        }
                    ],
                },
                "matchConditions": [
                    {
                        "name": "fixture-daily-trigger",
                        "expression": "object.data.state in ['requested','running']",
                    }
                ],
                "validations": [
                    {
                        "expression": "params.data.slot == object.data.slot && params.data.configurationSHA == object.data.configurationSHA && params.data.bundleSHA256 == object.data.bundleSHA256 && params.data.partitionSHA.matches('^[a-f0-9]{64}$')",
                        "message": "Fixture approval is incomplete for this daily receipt.",
                    }
                ],
            },
        }
        yield {
            "apiVersion": api,
            "kind": "ValidatingAdmissionPolicyBinding",
            "metadata": {"name": part},
            "spec": {
                "validationActions": ["Deny"],
                "policyName": part,
                "paramRef": {"name": part, "namespace": namespace, "parameterNotFoundAction": "Deny"},
            },
        }
    metadata = {"name": "fixture-publish-approvals", "namespace": namespace}
    yield {
        "apiVersion": role + "/v1",
        "kind": "Role",
        "metadata": metadata,
        "rules": [
            {
                "apiGroups": [""],
                "resources": ["configmaps"],
                "verbs": ["get", "patch"],
                "resourceNames": list(PARTS),
            }
        ],
    }
    yield {
        "apiVersion": role + "/v1",
        "kind": "RoleBinding",
        "metadata": metadata,
        "roleRef": {"apiGroup": role, "kind": "Role", "name": metadata["name"]},
        "subjects": [{"apiGroup": role, "kind": "User", "name": preparer}],
    }
