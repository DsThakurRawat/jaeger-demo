# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Reconcile one existing application release from the already verified private bundle."""

import os
from pathlib import Path
import subprocess
import tempfile

import yaml

from adoption_bundle import BundleError, RELEASES


def reconcile_existing(row, method, files, *, kubeconfig, context):
    """Invoke under the pinned toolchain, within consume() and its deployment lock."""
    key, options = row["id"], row["helm"]
    if key not in RELEASES or key.endswith("/demo-resources") or options["operation"] != "upgrade":
        raise BundleError("A frozen upgrade of an existing application release is required")
    if (method not in ("apply", "sync") or options["createNamespace"] is not False
            or options["wait"] not in ("legacy", "watcher")
            or any(type(options[k]) is not bool for k in ("atomic", "skipCRDs"))
            or any(type(options[k]) is not int or options[k] < 1 for k in ("timeoutSeconds", "historyMax"))):
        raise BundleError("Unexpected Helmfile operation or Helm options")
    ns, name = key.split("/")
    config = Path(kubeconfig).resolve(strict=True)
    tools = Path(os.environ.get("JAEGER_HELMFILE_TOOLCHAIN", ""))
    helm, helmfile = tools / "bin/helm", tools / "bin/helmfile"
    if not tools.is_absolute() or not all(path.is_file() for path in (helm, helmfile)):
        raise BundleError("Run the reconciler under the pinned Helmfile toolchain")
    with tempfile.TemporaryDirectory(prefix="jaeger-frozen-helm-") as scratch:
        root = Path(scratch)
        for member in ("chart.tgz", "values.yaml", "postrender.yaml"):
            with os.fdopen(os.open(root / member, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
                out.write(files[member])
        state = {
            "helmDefaults": {"historyMax": options["historyMax"], "createNamespace": False, "timeout": options["timeoutSeconds"]},
            "releases": [{"name": name, "namespace": ns, "chart": "./chart.tgz"}],
        }
        (root / "helmfile.yaml").write_text(yaml.safe_dump(state))
        env = {
            "PATH": str(tools / "bin") + os.pathsep + os.environ["PATH"], "HOME": scratch, "TMPDIR": scratch,
            "HELMFILE_TEMPDIR": scratch, "HELM_PLUGINS": str(tools / "plugins"), "KUBECONFIG": str(config),
            "HELM_CONFIG_HOME": str(root / "helm-config"), "HELM_DATA_HOME": str(root / "helm-data"),
            "HELM_CACHE_HOME": str(root / "helm-cache"), "HELM_DRIVER": "secret",
            "JAEGER_APPROVED_MANIFEST": str(root / "postrender.yaml"),
        }
        # Helmfile prepends --install even when reconciling a proven existing release.
        # The later flag must disable that fallback at Helm's actual mutation boundary.
        # Pass values directly to Helm so Helmfile cannot evaluate ref+ strings as remote secrets.
        sync = ["--install=false", "--values=./values.yaml", "--wait=" + options["wait"],
                "--rollback-on-failure=" + str(options["atomic"]).lower(),
                "--skip-crds=" + str(options["skipCRDs"]).lower(), "--create-namespace=false"]
        command = [helmfile, "--helm-binary", helm, "--file", "helmfile.yaml", "--kubeconfig", str(config),
                   "--kube-context", context, "--skip-deps", method, "--concurrency", "1", "--reset-values",
                   "--post-renderer", "verify-frozen-manifests", "--sync-args=" + " ".join(sync)]
        if method == "apply":
            command.extend(["--diff-args=--allow-unreleased=false --values=./values.yaml", "--suppress-secrets", "--suppress-diff"])
        # Helmfile and helm-diff can include config values and Secret data in either stream.
        result = subprocess.run(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode:
            raise BundleError(f"Frozen Helmfile {method} failed for {key} (exit {result.returncode})")
