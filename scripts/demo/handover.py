#!/usr/bin/env python3

# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Read-only prerequisites for the reviewed, existing-release script handover."""

import json
import os
import re
import subprocess
import sys


STATE = "jaeger-demo-deployment-revisions"
COMMON_OBJECTS = {
    "deployment": ("jaeger", "jaeger-hotrod", "trace-generator"),
    "service": ("jaeger-agent", "jaeger-collector", "jaeger-query", "jaeger-hotrod"),
    "configmap": ("user-config", "ui-config", "trace-script"),
    "serviceaccount": ("jaeger", "jaeger-hotrod"),
}


def validate_inputs(env, stack, mode, scope):
    """The existing reviewed-SHA input selects this restricted handover path."""
    expected = env.get("EXPECTED_CONFIG_SHA", "")
    if not expected:
        return False
    if not re.fullmatch(r"[0-9a-f]{40}", expected) or expected != env.get("GITHUB_SHA"):
        raise ValueError("Handover requires the matching reviewed configuration SHA")
    if (env.get("GITHUB_EVENT_NAME") != "workflow_dispatch" or env.get("GITHUB_REF") != "refs/heads/main"
            or stack not in ("oci", "otel") or mode != "upgrade" or scope != "jaeger"):
        raise ValueError("Handover requires a manual main/single-stack/upgrade/jaeger deployment")
    for image in ("JAEGER", "HOTROD"):
        if not re.fullmatch(r"[0-9a-f]{40}", env.get(f"JAEGER_DEMO_{image}_IMAGE_TAG", "")):
            raise ValueError(f"Handover requires an explicit full {image} image commit tag")
    if env.get("JAEGER_OTEL_DEMO_OPENSEARCH_RECOVERY", "required") != "required":
        raise ValueError("Handover cannot waive OpenSearch recovery")
    return True


def read(args):
    return subprocess.check_output(args, text=True)


def required_objects(stack):
    namespace = "default" if stack == "oci" else "jaeger"
    objects = [(namespace, kind, name) for kind, names in COMMON_OBJECTS.items() for name in names]
    objects.append(("default", "configmap", STATE))
    objects.append((namespace, "ingress", "jaeger-demo-ingress"))
    if stack == "oci":
        objects.append(("default", "ingress", "jaeger-root-redirect"))
    else:
        objects.extend([
            ("jaeger", "service", "jaeger-query-clusterip"),
            ("jaeger", "serviceaccount", "jaeger-es-index-cleaner"),
            ("jaeger", "cronjob", "jaeger-es-index-cleaner"),
            ("opensearch", "ingress", "opensearch-demo-ingress"),
            ("otel-demo", "ingress", "otel-demo-ingress"),
        ])
    return objects


def check_helm_context(env):
    if env.get("HELM_DRIVER", "secret") not in ("", "secret", "secrets"):
        raise ValueError("Hosted upgrades require Helm Secret storage")
    if env.get("HELM_KUBECONTEXT"):
        raise ValueError("Hosted upgrades require Helm to use the current kubectl context")
    namespace = read(["kubectl", "config", "view", "--minify", "-o", "jsonpath={..namespace}"]).strip()
    if namespace not in ("", "default") or env.get("HELM_NAMESPACE", "default") not in ("", "default"):
        raise ValueError("Hosted upgrades require the implicit kubectl and Helm namespace to be default")


def preflight(env, stack, mode, scope):
    if not validate_inputs(env, stack, mode, scope):
        return
    check_helm_context(env)
    # Both releases must be healthy before either ownership transfer. Never infer
    # absence from a denied read or let OCI's reinstall fallback handle this path.
    for namespace in ("default", "jaeger"):
        release = json.loads(read(["helm", "status", "jaeger", "--namespace", namespace, "-o", "json"]))
        if (release.get("name") != "jaeger" or release.get("namespace") != namespace
                or release.get("info", {}).get("status") != "deployed"
                or not isinstance(release.get("version"), int) or release["version"] < 1):
            raise ValueError(f"Handover requires an existing deployed {namespace}/jaeger release")
    # Missing objects would turn an accepted patch into an unreviewed create.
    # The frozen record separately compares manifests, hooks and prior revisions.
    for namespace, kind, name in required_objects(stack):
        if not read(["kubectl", "-n", namespace, "get", kind, name, "-o", "name"]).strip():
            raise ValueError(f"Handover requires existing {namespace}/{kind}/{name}")


if __name__ == "__main__":
    preflight(dict(os.environ), *sys.argv[1:])
