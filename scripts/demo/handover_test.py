# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import json
import subprocess
import sys
import unittest
from unittest.mock import patch

import handover


class HandoverTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "EXPECTED_CONFIG_SHA": "a" * 40, "GITHUB_SHA": "a" * 40,
            "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
            "JAEGER_DEMO_JAEGER_IMAGE_TAG": "b" * 40,
            "JAEGER_DEMO_HOTROD_IMAGE_TAG": "c" * 40,
        }

    @staticmethod
    def response(args):
        if args[:3] == ["kubectl", "config", "view"]:
            return "default"
        if args[0] == "helm":
            return json.dumps({"name": "jaeger", "namespace": args[4], "version": 22,
                               "info": {"status": "deployed"}})
        return args[4] + "/" + args[5]

    @patch("handover.read")
    def test_ordinary_operations_do_not_use_handover_preflight(self, read):
        self.assertFalse(handover.validate_inputs({}, "all", "clean", "all"))
        handover.preflight({}, "all", "clean", "all")
        read.assert_not_called()

    def test_accepts_only_explicit_manual_single_stack_jaeger_upgrades(self):
        for stack in ("oci", "otel"):
            self.assertTrue(handover.validate_inputs(self.env, stack, "upgrade", "jaeger"))
        for stack, mode, scope in (("all", "upgrade", "jaeger"), ("oci", "clean", "jaeger"),
                                   ("oci", "upgrade", "monitoring"), ("otel", "upgrade", "all")):
            with self.subTest(route=(stack, mode, scope)), self.assertRaises(ValueError):
                handover.validate_inputs(self.env, stack, mode, scope)
        for key, value in (("EXPECTED_CONFIG_SHA", "main"), ("GITHUB_SHA", "d" * 40),
                           ("GITHUB_EVENT_NAME", "schedule"), ("GITHUB_REF", "refs/heads/feature"),
                           ("JAEGER_DEMO_JAEGER_IMAGE_TAG", ""), ("JAEGER_DEMO_HOTROD_IMAGE_TAG", "latest")):
            with self.subTest(key=key, value=value), patch("handover.read") as read:
                with self.assertRaises(ValueError):
                    handover.preflight(dict(self.env, **{key: value}), "oci", "upgrade", "jaeger")
                read.assert_not_called()

    @patch("handover.read")
    def test_checks_both_release_states_and_exact_existing_object_sets(self, read):
        read.side_effect = self.response
        for stack, namespace, count in (("oci", "default", 15), ("otel", "jaeger", 19)):
            with self.subTest(stack=stack):
                read.reset_mock()
                handover.preflight(self.env, stack, "upgrade", "jaeger")
                calls = [call.args[0] for call in read.call_args_list]
                self.assertEqual(["default", "jaeger"], [call[4] for call in calls if call[0] == "helm"])
                objects = calls[3:]
                self.assertEqual(count, len(objects))
                self.assertTrue(all(call[0] == "kubectl" and call[3] == "get" for call in objects))
                self.assertTrue(all(call[-2:] == ["-o", "name"] for call in objects))
                self.assertIn(["kubectl", "-n", "default", "get", "configmap", handover.STATE, "-o", "name"], objects)
                self.assertIn(["kubectl", "-n", namespace, "get", "deployment", "trace-generator", "-o", "name"], objects)
                if stack == "otel":
                    for ns, name in (("opensearch", "opensearch-demo-ingress"), ("otel-demo", "otel-demo-ingress")):
                        self.assertIn(["kubectl", "-n", ns, "get", "ingress", name, "-o", "name"], objects)

    @patch("handover.read")
    def test_missing_failed_pending_or_unreadable_release_fails_before_object_checks(self, read):
        for namespace in ("default", "jaeger"):
            for status in ("", "failed", "pending-install", "pending-upgrade", "pending-rollback", "uninstalling"):
                with self.subTest(namespace=namespace, status=status):
                    def response(args):
                        if args[0] == "helm" and args[4] == namespace:
                            return json.dumps({"name": "jaeger", "namespace": namespace, "version": 1,
                                               "info": {"status": status}})
                        return self.response(args)
                    read.reset_mock()
                    read.side_effect = response
                    with self.assertRaisesRegex(ValueError, "existing deployed"):
                        handover.preflight(self.env, "otel", "upgrade", "jaeger")
                    self.assertFalse(any(call.args[0][3:4] == ["get"] for call in read.call_args_list))
        for error in (subprocess.CalledProcessError(1, "helm", stderr="Forbidden"),
                      subprocess.CalledProcessError(1, "helm", stderr="release: not found")):
            read.side_effect = ["default", error]
            with self.assertRaises(subprocess.CalledProcessError):
                handover.preflight(self.env, "oci", "upgrade", "jaeger")

    @patch("handover.read")
    def test_wrong_release_identity_or_revision_is_rejected(self, read):
        for change in ({"name": "other"}, {"namespace": "other"}, {"version": 0}, {"version": "22"}):
            release = {"name": "jaeger", "namespace": "default", "version": 22, "info": {"status": "deployed"}}
            read.side_effect = ["default", json.dumps(dict(release, **change))]
            with self.subTest(change=change), self.assertRaises(ValueError):
                handover.preflight(self.env, "oci", "upgrade", "jaeger")

    @patch("handover.read")
    def test_missing_or_forbidden_object_cannot_be_treated_as_existing(self, read):
        for stack in ("oci", "otel"):
            for missing in handover.required_objects(stack):
                for error in (False, True):
                    with self.subTest(stack=stack, missing=missing, forbidden=error):
                        def response(args):
                            if args[:1] == ["kubectl"] and args[1:2] == ["-n"] and (args[2], args[4], args[5]) == missing:
                                if error:
                                    raise subprocess.CalledProcessError(1, args, stderr="Forbidden")
                                return ""
                            return self.response(args)
                        read.side_effect = response
                        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                            handover.preflight(self.env, stack, "upgrade", "jaeger")

    @patch("handover.read")
    def test_namespace_and_storage_overrides_are_rejected_before_release_access(self, read):
        for env, namespace in ((dict(self.env, HELM_DRIVER="configmap"), "default"),
                               (dict(self.env, HELM_NAMESPACE="other"), "default"), (self.env, "other")):
            read.reset_mock()
            read.return_value = namespace
            with self.subTest(env=env, namespace=namespace), self.assertRaises(ValueError):
                handover.preflight(env, "oci", "upgrade", "jaeger")
            self.assertFalse(any(call.args[0][0] == "helm" for call in read.call_args_list))

    def test_reader_propagates_command_failures(self):
        self.assertEqual("ready\n", handover.read([sys.executable, "-c", "print('ready')"]))
        with self.assertRaises(subprocess.CalledProcessError):
            handover.read([sys.executable, "-c", "raise SystemExit(7)"])


if __name__ == "__main__":
    unittest.main()
