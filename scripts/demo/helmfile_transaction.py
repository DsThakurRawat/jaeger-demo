# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Reconcile one release from the already verified private bundle."""

import contextlib
import os
from pathlib import Path
import re
import subprocess
import tempfile

import yaml

from adoption_bundle import BundleError, RELEASES


COMPANION = "/demo-resources"
# Helm treats an empty --kube-context as unset and uses the kubeconfig's current context.
CONTEXT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:@/=+-]{0,252}")


def check_options(method, options):
    if (method not in ("apply", "sync") or options["createNamespace"] is not False
            or options["wait"] not in ("legacy", "watcher")
            or any(type(options[k]) is not bool for k in ("atomic", "skipCRDs"))
            or any(type(options[k]) is not int or options[k] < 1 for k in ("timeoutSeconds", "historyMax"))):
        raise BundleError("Unexpected Helmfile operation or Helm options")


@contextlib.contextmanager
def private_inputs(files, kubeconfig, context):
    """Yield the pinned tools, an owner-only scratch directory with the frozen inputs and a minimal environment."""
    if not isinstance(context, str) or not CONTEXT.fullmatch(context):
        raise BundleError("A named kubeconfig context is required")
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
        env = {
            "PATH": str(tools / "bin") + os.pathsep + os.environ["PATH"], "HOME": scratch, "TMPDIR": scratch,
            "HELMFILE_TEMPDIR": scratch, "HELM_PLUGINS": str(tools / "plugins"), "KUBECONFIG": str(config),
            "HELM_CONFIG_HOME": str(root / "helm-config"), "HELM_DATA_HOME": str(root / "helm-data"),
            "HELM_CACHE_HOME": str(root / "helm-cache"), "HELM_DRIVER": "secret",
            "JAEGER_APPROVED_MANIFEST": str(root / "postrender.yaml"),
        }
        yield helm, helmfile, config, root, env


def execute(command, root, env, description):
    # Helm, Helmfile and helm-diff can include config values and Secret data in either stream.
    result = subprocess.run(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        raise BundleError(f"{description} (exit {result.returncode})")


def upgrade(key, method, options, files, kubeconfig, context):
    ns, name = key.split("/")
    with private_inputs(files, kubeconfig, context) as (helm, helmfile, config, root, env):
        state = {
            "helmDefaults": {"historyMax": options["historyMax"], "createNamespace": False, "timeout": options["timeoutSeconds"]},
            "releases": [{"name": name, "namespace": ns, "chart": "./chart.tgz"}],
        }
        (root / "helmfile.yaml").write_text(yaml.safe_dump(state))
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
        execute(command, root, env, f"Frozen Helmfile {method} failed for {key}")


def reconcile_existing(row, method, files, *, kubeconfig, context):
    """Invoke under the pinned toolchain, within consume() and its deployment lock."""
    key, options = row["id"], row["helm"]
    if key not in RELEASES or key.endswith(COMPANION) or options["operation"] != "upgrade":
        raise BundleError("A frozen upgrade of an existing application release is required")
    check_options(method, options)
    upgrade(key, method, options, files, kubeconfig, context)


def reconcile_companion(row, method, files, *, kubeconfig, context):
    """Install or upgrade one namespace-local companion release from its frozen inputs.

    Invoke under the pinned toolchain, within consume() and its deployment lock,
    after the reviewed ownership patches. The first install runs the pinned Helm
    directly. Helm's upgrade --install cannot require an absent release, whereas
    install fails on any stored record of that name that Helm can read. Without
    --take-ownership it also refuses an existing object that lacks this release's
    ownership metadata.
    """
    key, options = row["id"], row["helm"]
    if key not in RELEASES or not key.endswith(COMPANION) or options["operation"] not in ("install", "upgrade"):
        raise BundleError("A frozen companion release operation is required")
    check_options(method, options)
    if options["operation"] == "upgrade":
        # An installed companion is reconciled exactly like an existing application release.
        upgrade(key, method, options, files, kubeconfig, context)
        return
    if method != "apply" or options["atomic"] is not False:
        raise BundleError("The first companion install must be a non-atomic apply")
    ns, name = key.split("/")
    with private_inputs(files, kubeconfig, context) as (helm, _, config, root, env):
        command = [helm, "install", name, "./chart.tgz", "--namespace", ns, "--kubeconfig", str(config),
                   "--kube-context", context, "--values=./values.yaml", "--post-renderer", "verify-frozen-manifests",
                   "--wait=" + options["wait"], "--timeout", f"{options['timeoutSeconds']}s",
                   "--rollback-on-failure=false", "--skip-crds=" + str(options["skipCRDs"]).lower(),
                   "--create-namespace=false"]
        execute(command, root, env, f"Frozen Helm install failed for {key}")


def reconcile(row, method, files, *, kubeconfig, context):
    """Dispatch one consume() callback. A bundle holds either companion or application releases."""
    callback = reconcile_companion if str(row["id"]).endswith(COMPANION) else reconcile_existing
    callback(row, method, files, kubeconfig=kubeconfig, context=context)
