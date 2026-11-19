# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""The five public Ingresses retain their routing and provider-owned settings."""

import copy
import unittest

import scheduled_adapter as adapter
import scheduled_approvals as approvals


class PublicIngressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.routes = [
            obj
            for key in ("oci-jaeger", "otel-jaeger")
            for obj in adapter.supplemental_for(key, adapter.ROOT)
            if obj["kind"] == "Ingress"
        ]

    def test_checkout_routes_and_server_metadata(self):
        self.assertEqual(len(self.routes), 5)
        self.assertEqual(
            {obj["metadata"]["namespace"] for obj in self.routes},
            {"default", "jaeger", "otel-demo", "opensearch"},
        )
        for obj in self.routes:
            after = copy.deepcopy(obj)
            after["metadata"]["resourceVersion"] = "34"
            after["status"] = {"loadBalancer": {"ingress": [{"hostname": "fixture.invalid"}]}}
            approvals.safe_transition(obj, after)

    def test_preparation_preserves_complete_spec_and_provider_metadata(self):
        for original in self.routes:
            with self.subTest(ingress=original["metadata"]["name"]):
                issuer = next(
                    key for key in original["metadata"]["annotations"] if key.startswith("cert-manager.io/")
                )
                for field, patch in (
                    ("class", lambda o: o["spec"].update(ingressClassName="unreviewed")),
                    (
                        "default backend",
                        lambda o: o["spec"].update(
                            defaultBackend={"service": {"name": "other", "port": {"number": 81}}}
                        ),
                    ),
                    ("host", lambda o: o["spec"]["rules"][0].update(host="other.invalid")),
                    ("path", lambda o: o["spec"]["rules"][0]["http"]["paths"][0].update(path="/other")),
                    (
                        "match type",
                        lambda o: o["spec"]["rules"][0]["http"]["paths"][0].update(
                            pathType="ImplementationSpecific"
                        ),
                    ),
                    (
                        "backend",
                        lambda o: o["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"].update(
                            name="other"
                        ),
                    ),
                    (
                        "port",
                        lambda o: o["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"].update(
                            port={"number": 81}
                        ),
                    ),
                    ("TLS host", lambda o: o["spec"]["tls"][0].update(hosts=["other.invalid"])),
                    ("TLS Secret", lambda o: o["spec"]["tls"][0].update(secretName="other-tls")),
                    ("issuer removal", lambda o: o["metadata"]["annotations"].pop(issuer)),
                    (
                        "issuer replacement",
                        lambda o: o["metadata"]["annotations"].update({issuer: "different"}),
                    ),
                    (
                        "provider settings",
                        lambda o: o["metadata"]["annotations"].update(
                            {"external-dns.alpha.kubernetes.io/target": "other.invalid"}
                        ),
                    ),
                    (
                        "redirect",
                        lambda o: o["metadata"]["annotations"].update(
                            {"nginx.ingress.kubernetes.io/permanent-redirect": "https://other.invalid/"}
                        ),
                    ),
                    ("provider label", lambda o: o["metadata"].update(labels={"controller": "different"})),
                ):
                    with self.subTest(field=field):
                        after = copy.deepcopy(original)
                        patch(after)
                        with self.assertRaisesRegex(ValueError, "Public Ingress"):
                            approvals.safe_transition(original, after)

    def test_native_ingress_admission_keeps_spec_and_metadata_content_neutral(self):
        from pathlib import Path
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "ops/executor"))
        import permissions

        resources, _ = permissions.regular(daily=True)
        policies = {o["metadata"]["name"]: o for o in resources if o["kind"] == "ValidatingAdmissionPolicy"}
        guarded = set()
        for binding in resources:
            spec = binding.get("spec", {})
            for rule in spec.get("matchResources", {}).get("resourceRules", []):
                if rule["resources"] != ["ingresses"]:
                    continue
                scope = approvals.t.namespace(spec["paramRef"]["name"].removeprefix("daily-approved-"))
                guarded.update((scope, name) for name in rule["resourceNames"])
                expression = policies[spec["policyName"]]["spec"]["validations"][-1]["expression"]
                self.assertNotIn("params.spec.changes", expression)
                self.assertIn(permissions.same_except("oldObject", "object", ["metadata"]), expression)
                self.assertIn(
                    permissions.same_except(
                        "oldObject.metadata",
                        "object.metadata",
                        ["managedFields", "resourceVersion", "generation"],
                    ),
                    expression,
                )
        self.assertEqual(guarded, {(o["metadata"]["namespace"], o["metadata"]["name"]) for o in self.routes})
