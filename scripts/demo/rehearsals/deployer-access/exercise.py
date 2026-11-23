#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Exercise the committed deployer roles against a real API server.

With --assets (pinned envtest kube-apiserver, etcd and kubectl) this boots a
private, loopback-only disposable API server. With --kubeconfig/--context it
uses that cluster instead, which is how the kind-based CI job calls it.

Either way it:

1. Applies ops/rbac/deployer.yaml, proving the API server accepts every rule,
   including the empty resource name on the escalate rules.
2. Binds two fixture identities with the committed `deployer_access.py
   bindings` output and runs the committed `deployer_access.py check` for both
   against live SubjectAccessReview answers, proving the cluster's answers equal
   the offline model for every required and denied request.
3. Installs the pinned monitoring chart as the administrator (production
   releases pre-exist; the deployer identity only ever upgrades) and then runs
   `helm upgrade` for it as the scheduled identity holding only the deployer
   role. Without --wait-upgrade the upgrade is a server dry-run (the API server
   still authorizes every write, but no kubelet exists to complete hooks or
   readiness) and the hook objects are created and deleted for real; with
   --wait-upgrade the upgrade is a complete `helm upgrade --wait`, hooks
   included.

The output receipt is public: identities and versions only, no credentials.
"""

import argparse
import contextlib
import csv
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

import yaml

ROOT = Path(__file__).resolve().parents[4]
ROLES = ROOT / "ops/rbac/deployer.yaml"
VALUES = ROOT / "examples/oci/monitoring-values.yaml"
CHECK = ROOT / "scripts/demo/deployer_access.py"
SCHEDULED = "ocid1.user.oc1..fixturescheduled"
MANUAL = "ocid1.user.oc1..fixturemanual"
RELEASE_NS = "default"
# The production values enable the CRD upgrade hook on every upgrade.
EXTRA_VALUES = ["crds.upgradeJob.enabled=true", "crds.upgradeJob.forceConflicts=true"]


def now():
    return datetime.now(timezone.utc).isoformat()


def free_ports():
    with contextlib.ExitStack() as stack:
        handles = [stack.enter_context(socket.socket()) for _ in range(3)]
        for handle in handles:
            handle.bind(("127.0.0.1", 0))
        return [h.getsockname()[1] for h in handles]


def terminate(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(12)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)


def hook_documents(text):
    """Rendered objects that carry a helm.sh/hook annotation."""
    hooks = []
    for doc in yaml.safe_load_all(text):
        if doc and ((doc.get("metadata") or {}).get("annotations") or {}).get("helm.sh/hook"):
            hooks.append(doc)
    return hooks


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--assets", type=Path, help="Directory with envtest kube-apiserver, etcd and kubectl")
    parser.add_argument("--kubeconfig", type=Path, help="Existing cluster fixture (the kind-based CI job)")
    parser.add_argument("--context", help="Context to select with --kubeconfig")
    parser.add_argument("--chart", type=Path, required=True, help="Pinned kube-prometheus-stack archive")
    parser.add_argument("--roles", type=Path, default=ROLES, help="Role manifest to apply; defaults to the committed roles")
    parser.add_argument("--wait-upgrade", action="store_true",
                        help="Run a complete `helm upgrade --wait`; needs kubelets (kind), not envtest")
    parser.add_argument("--receipt", type=Path, required=True, help="Where the public evidence JSON is written")
    args = parser.parse_args()
    if bool(args.assets) == bool(args.kubeconfig):
        parser.error("Supply exactly one of --assets or --kubeconfig")
    if bool(args.kubeconfig) != bool(args.context):
        parser.error("--kubeconfig and --context go together")
    if args.wait_upgrade and args.assets:
        parser.error("--wait-upgrade needs kubelets; point --kubeconfig at a kind cluster")
    receipt = args.receipt.absolute()
    if receipt.is_symlink() or receipt.exists():
        parser.error("Choose a new receipt path.")
    if not __debug__:
        parser.error("Use the normal Python interpreter so rehearsal assertions stay enabled.")

    def on_termination(_signal, _frame):
        raise SystemExit("Interrupted during local rehearsal")

    signal.signal(signal.SIGTERM, on_termination)
    helm = shutil.which("helm")
    assert helm, "The lock-pinned Helm must be on PATH; run through scripts/demo/helmfile_tools.py run"
    lock = Path(__file__).resolve().parents[4] / "scripts/demo/helmfile-tools.lock.json"
    pinned = json.loads(lock.read_bytes())["tools"]["helm"]["version"]
    probe = subprocess.run([helm, "version", "--template", "{{.Version}}"],
                           capture_output=True, check=True, text=True)
    found = probe.stdout.strip()
    assert found == pinned, f"Helm {pinned} from {lock.name} is required, found {found}"
    chart = args.chart.resolve()
    chart_sha = hashlib.sha256(chart.read_bytes()).hexdigest()
    report = dict(
        startedAt=now(),
        productionAccess=False,
        chart=dict(name=chart.name, sha256=chart_sha),
        mode="helm upgrade --wait" if args.wait_upgrade else "server dry-run upgrade with a real hook lifecycle",
        steps=[],
        negativeTests=[],
    )
    with tempfile.TemporaryDirectory(prefix="deployer-access-", dir=receipt.parent) as temp, \
            contextlib.ExitStack() as stack:
        root = Path(temp)
        cfg = root / "fixture.kubeconfig"
        env = dict(
            PATH=os.environ["PATH"],
            KUBECONFIG=str(args.kubeconfig or cfg),
            HELM_DRIVER="secret",
            HELM_CONFIG_HOME=str(root / "helm-config"),
            HELM_CACHE_HOME=str(root / "helm-cache"),
            HELM_DATA_HOME=str(root / "helm-data"),
            TMPDIR=str(root),
            NO_PROXY="127.0.0.1,localhost",
        )

        def execute(cmd, body=None, check=True, timeout=90, **kw):
            p = subprocess.run(list(map(str, cmd)), input=body, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               env=kw.pop("env", env), cwd=root, timeout=timeout, **kw)
            if check and p.returncode:
                raise RuntimeError(f"{Path(str(cmd[0])).name} {cmd[1]} failed ({p.returncode}): {(p.stderr + p.stdout)[-1300:]}")
            return p

        kubectl = None

        def kube(cmd, user=None, **kw):
            as_user = ["--as", user, "--as-group", "system:authenticated"] if user else []
            base = [kubectl, "--kubeconfig", env["KUBECONFIG"]]
            if args.context:
                base += ["--context", args.context]
            return execute([*base, *as_user, *cmd], **kw)

        def helm_as(cmd, user=None, **kw):
            as_user = ["--kube-as-user", user, "--kube-as-group", "system:authenticated"] if user else []
            base = [helm, "--kubeconfig", env["KUBECONFIG"]]
            if args.context:
                base += ["--kube-context", args.context]
            return execute([*base, *as_user, *cmd], **kw)

        def run_server(cmd, name):
            stream = stack.enter_context((root / f"{name}.log").open("w"))
            proc = subprocess.Popen(list(map(str, cmd)), env=env, cwd=root, stdout=stream, stderr=stream)
            stack.callback(terminate, proc)
            return proc

        if args.assets:
            apiserver, etcd, kubectl = [str(args.assets.resolve() / x) for x in ("kube-apiserver", "etcd", "kubectl")]
            openssl = shutil.which("openssl")
            assert openssl, "openssl must be on PATH"
            old_umask = os.umask(0o077)
            cert, key, sa_key, tokens = [root / p for p in ("tls.pem", "tls.key", "service-account.key", "tokens.csv")]
            execute([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-sha256", "-days", "1",
                     "-subj", "/CN=localhost", "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
                     "-keyout", key, "-out", cert])
            execute([openssl, "genrsa", "-out", sa_key, "2048"])
            token = secrets.token_urlsafe(48)
            with tokens.open("w", newline="") as fp:
                csv.writer(fp).writerow((token, "fixture-local-admin", "fixture-admin", "system:masters"))
            port, peer, api = free_ports()
            endpoint = f"http://127.0.0.1:{port}"
            server = f"https://127.0.0.1:{api}"
            run_server([etcd, "--name", "default", "--data-dir", root / "etcd",
                        "--listen-client-urls", endpoint, "--advertise-client-urls", endpoint,
                        "--listen-peer-urls", f"http://127.0.0.1:{peer}",
                        "--initial-advertise-peer-urls", f"http://127.0.0.1:{peer}",
                        "--initial-cluster", f"default=http://127.0.0.1:{peer}", "--log-level", "error"], "etcd")
            api_proc = run_server([apiserver, "--etcd-servers", endpoint, "--secure-port", str(api),
                                   "--bind-address", "127.0.0.1", "--advertise-address", "127.0.0.1",
                                   "--endpoint-reconciler-type", "none",
                                   "--service-cluster-ip-range", "10.226.0.0/24",
                                   "--tls-cert-file", cert, "--tls-private-key-file", key,
                                   "--cert-dir", root / "certs", "--token-auth-file", tokens,
                                   "--authorization-mode=RBAC", "--anonymous-auth=false",
                                   "--service-account-signing-key-file", sa_key,
                                   "--service-account-key-file", sa_key,
                                   "--service-account-issuer", "https://fixture.invalid"], "apiserver")
            cfg.write_text(json.dumps(dict(
                apiVersion="v1", kind="Config",
                clusters=[dict(name="fixture", cluster={"server": server, "certificate-authority": str(cert)})],
                users=[dict(name="admin", user={"token": token})],
                contexts=[dict(name="fixture", context={"cluster": "fixture", "user": "admin"})],
                **{"current-context": "fixture"})))
            env["KUBECONFIG"] = str(cfg)
            args.context = None
            for _ in range(90):
                if kube(["get", "--raw", "/readyz"], check=False, timeout=6).returncode == 0:
                    break
                if api_proc.poll() is not None:
                    raise RuntimeError("Local API startup failed: " + (root / "apiserver.log").read_text()[-1800:])
                time.sleep(0.4)
            else:
                raise RuntimeError("Local API timeout: " + (root / "apiserver.log").read_text()[-1500:])
            os.umask(old_umask)
            report["endpoint"] = "ephemeral 127.0.0.1"
        else:
            kubectl = shutil.which("kubectl")
            assert kubectl, "kubectl must be on PATH with --kubeconfig"
            report["endpoint"] = "provided cluster fixture"

        report["versions"] = dict(
            helm=execute([helm, "version", "--short"]).stdout.strip(),
            kubectl=json.loads(execute([kubectl, "version", "--client", "-o", "json"]).stdout)["clientVersion"]["gitVersion"],
            apiServer=json.loads(kube(["version", "-o", "json"]).stdout)["serverVersion"]["gitVersion"],
        )

        for namespace in ("jaeger", "opensearch", "otel-demo"):
            kube(["create", "namespace", namespace], check=False)

        # 1. The API server accepts the committed roles.
        kube(["apply", "-f", str(args.roles)])
        report["steps"].append("roles applied to the live API server")

        # 2. Bind the fixture identities with the committed generator and
        # check both against live SubjectAccessReview answers.
        bindings = execute([sys.executable, str(CHECK), "bindings",
                            "--manual-user", MANUAL, "--scheduled-user", SCHEDULED]).stdout
        kube(["apply", "-f", "-"], body=bindings)
        report["steps"].append("fixture identities bound through deployer_access.py bindings")
        for identity, user in (("scheduled", SCHEDULED), ("manual", MANUAL)):
            result = execute([sys.executable, str(CHECK), "check", "--identity", identity, "--user", user],
                             env={**env, "PATH": str(Path(kubectl).parent) + ":" + env["PATH"]})
            assert f"{identity}: 0 difference(s)" in result.stdout, result.stdout
        report["steps"].append("live SubjectAccessReview answers equal the offline model for every required and denied request")

        def can_i(user, verb, resource, namespace=None):
            cmd = ["auth", "can-i", verb, resource]
            if namespace:
                cmd += ["-n", namespace]
            return kube(cmd, user=user, check=False).stdout.strip()

        assert can_i(SCHEDULED, "get", "namespaces/jaeger") == "yes"
        assert can_i(SCHEDULED, "list", "secrets", "kube-system") == "no"
        assert can_i(SCHEDULED, "bind", "clusterrole/cluster-admin") == "no"
        assert can_i(MANUAL, "get", "persistentvolumes") == "yes"
        assert can_i(SCHEDULED, "get", "persistentvolumes") == "no"
        report["negativeTests"].append("scheduled identity cannot reach kube-system Secrets, cluster-admin or PersistentVolumes")

        # 3. The pinned monitoring chart as an identity holding only this role.
        values = ["-f", str(VALUES)]
        for setting in EXTRA_VALUES:
            values += ["--set", setting]
        install = ["install", "prometheus", chart, "-n", RELEASE_NS, *values]
        if args.wait_upgrade:
            helm_as([*install, "--wait=legacy", "--timeout", "20m"], timeout=1350)
        else:
            # No kubelet completes hooks or readiness here; the two hook
            # producers stay off for the administrator's initial release and
            # the deployer's upgrade below proves their RBAC by server
            # dry-run and by a real create/delete lifecycle.
            helm_as([*install, "--set", "crds.upgradeJob.enabled=false",
                     "--set", "prometheusOperator.admissionWebhooks.enabled=false",
                     "--set", "prometheusOperator.admissionWebhooks.patch.enabled=false",
                     # No kubelet runs the certgen hook here, so no TLS secret
                     # can exist; the chart refuses TLS without one. The kind
                     # path keeps the production TLS setting.
                     "--set", "prometheusOperator.tls.enabled=false"], timeout=600)
        report["steps"].append("administrator installed the pinned monitoring chart (production releases pre-exist)")

        upgrade = ["upgrade", "prometheus", chart, "-n", RELEASE_NS, "--install=false", "--reset-values",
                   "--server-side=auto", *values]
        if args.wait_upgrade:
            helm_as([*upgrade, "--wait=legacy", "--timeout", "20m"], user=SCHEDULED, timeout=1350)
            report["steps"].append("helm upgrade --wait succeeded as the scheduled identity holding only the deployer role")
        else:
            helm_as([*upgrade, "--dry-run=server"], user=SCHEDULED, timeout=600)
            report["steps"].append("server dry-run upgrade authorized every write as the scheduled identity")
            rendered = helm_as(["template", "prometheus", chart, "-n", RELEASE_NS, *values], timeout=300).stdout
            hooks = hook_documents(rendered)
            kinds = sorted({doc["kind"] for doc in hooks})
            assert {"Job", "ClusterRole", "ClusterRoleBinding"} <= set(kinds), kinds
            for doc in hooks:
                kube(["create", "-f", "-"], user=SCHEDULED, body=json.dumps(doc))
            for doc in reversed(hooks):
                kube(["delete", "-f", "-"], user=SCHEDULED, body=json.dumps(doc))
            report["steps"].append("real create and delete of every rendered hook object as the scheduled identity: " + ", ".join(kinds))

        forbidden = helm_as(["install", "forbidden-probe", chart, "-n", "kube-system", "--dry-run=server"],
                            user=SCHEDULED, check=False, timeout=300)
        assert forbidden.returncode and "forbidden" in forbidden.stderr, forbidden.stderr[-400:]
        report["negativeTests"].append("the same chart install into kube-system is refused as the deployer identity")

        report.update(finishedAt=now(), passed=True)
        with receipt.open("x") as output:
            json.dump(report, output, indent=2)
            output.write("\n")
        print("PASS: deployer roles accepted, live access checks equal the offline model, "
              "monitoring upgrade authorized as the deployer identity")
        print(receipt)
        return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, AssertionError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
