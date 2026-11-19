# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

import json
from pathlib import Path
import tempfile
import unittest

from validate_helmfile import prepare, schema_node


class ManifestValidationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.manifest = self.root / "render.yaml"
        self.spec = {"definitions": {"crd": {"x-kubernetes-group-version-kind":
                    [{"group": "apiextensions.k8s.io", "version": "v1", "kind": "CustomResourceDefinition"}]}}}
        self.crd = """apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
spec:
  names: {kind: Alarm}
  group: example.com
  versions:
  - name: v1
    served: true
    schema:
      openAPIV3Schema:
        type: object
        properties:
          spec:
            type: object
            properties:
              operator: {type: string, enum: [=, '!=']}
"""

    def test_custom_resources_use_matching_served_group_and_version(self):
        cr = "apiVersion: example.com/v1\nkind: Alarm\n"
        self.manifest.write_text(cr + "---\n" + self.crd)
        prepare(self.spec, [self.manifest], self.root)
        schema = json.loads((self.root / "example.com/v1/alarm.json").read_text())
        spec = schema["properties"]["spec"]
        self.assertIs(spec["additionalProperties"], False)
        self.assertEqual(["=", "!="], spec["properties"]["operator"]["enum"])
        self.assertIn("ObjectMeta", schema["properties"]["metadata"]["$ref"])
        for manifest in (cr.replace("example.com/v1", "different.com/v1") + "---\n" + self.crd,
                         cr + "---\n" + self.crd.replace("served: true", "served: false")):
            self.manifest.write_text(manifest)
            with self.assertRaisesRegex(ValueError, "Missing schema"):
                prepare(self.spec, [self.manifest], self.root)

    def test_required_keys_and_free_form_data(self):
        obj = {"type": "object", "required": ["port"], "properties": {
            "port": {"type": "string", "format": "int-or-string"},
            "labels": {"type": "object", "additionalProperties": {"type": "string"}}}}
        schema = schema_node(obj)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(["port"], schema["required"])
        self.assertEqual(["integer", "string"], schema["properties"]["port"]["type"])
        labels, optional = schema["properties"]["labels"]["anyOf"]
        self.assertEqual({"type": "string"}, labels["additionalProperties"])
        self.assertEqual({"type": "null"}, optional)
        free = {"type": "object", "properties": {}, "x-kubernetes-preserve-unknown-fields": True}
        self.assertNotIn("additionalProperties", schema_node(free, custom=True))
        self.assertNotIn("anyOf", schema_node(obj, custom=True)["properties"]["labels"])

    def test_reject_duplicate_or_external_custom_schema(self):
        self.manifest.write_text(self.crd + "---\n" + self.crd)
        with self.assertRaisesRegex(ValueError, "Duplicate served schema"):
            prepare(self.spec, [self.manifest], self.root)
        for external in ({"$ref": "https://example.com/not-local.json"},
                         {"properties": {"config": {"patternProperties": {"x": {"$ref": "file:///invalid"}}}}}):
            with self.assertRaisesRegex(ValueError, "Unsupported structural CRD"):
                schema_node(external, custom=True)

    def test_missing_or_invalid_resource_identity_fails_closed(self):
        for text, error in (("# empty render", "No resources"),
                            ("apiVersion: v1/per/invalid\nkind: Widget\n", "Invalid resource identity"),
                            ("apiVersion: v1\nkind: MissingSchema\n", "Missing schema")):
            self.manifest.write_text(text)
            with self.assertRaisesRegex(ValueError, error):
                prepare(self.spec, [self.manifest], self.root)
