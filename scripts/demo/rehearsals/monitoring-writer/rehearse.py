# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Run the pinned true-hook fixture on the explicitly named local kind cluster only."""

import argparse
import base64
from datetime import datetime, timezone
import gzip
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import time
from urllib.parse import urlsplit

import yaml

from build_fixture import content_digest, REVIEWED, SEED

from tickets import TriggerFixture

CONTEXT = "kind-jaeger-hook-rehearsal"
NODE = "jaeger-hook-rehearsal-control-plane"
PREFIX = "prometheus-kube-prometheus-admission"
JOBS = {"prometheus-crds-upgrade", PREFIX + "-create", PREFIX + "-patch"}
SECRETS = {
    "prometheus-grafana",
    "alertmanager-prometheus-kube-prometheus-alertmanager",
    "prometheus-kube-prometheus-prometheus-scrape-confg",
}
MARKER = "jaegertracing.io/local-rehearsal-marker"


class Failure(Exception):
    pass


def require(condition, detail):
    if not condition:
        raise Failure(detail)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def utc():
    return datetime.now(timezone.utc).isoformat()


def when(value):
    normalized = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), value)
    return datetime.fromisoformat(normalized.replace("Z", "+00:00"))


def local_config(path, context):
    require(
        context == CONTEXT
        and path.is_absolute()
        and path.is_file()
        and not path.is_symlink(),
        "Explicit local kubeconfig/context required",
    )
    config = yaml.safe_load(path.read_bytes())
    require(
        config["current-context"] == context,
        "Unexpected current context in dedicated fixture kubeconfig",
    )
    for group in ("contexts", "clusters", "users"):
        require(
            len(config[group]) == 1 and config[group][0]["name"] == context,
            "Unexpected additional Kubernetes identities or clusters",
        )
    ctx = config["contexts"][0]["context"]
    require(
        ctx["cluster"] == context and ctx["user"] == context,
        "Context identity mismatch",
    )
    server = config["clusters"][0]["cluster"]
    require(
        set(server) == {"server", "certificate-authority-data"},
        "Unexpected local TLS trust override",
    )
    url = urlsplit(server["server"])
    require(
        url.scheme == "https"
        and url.port
        and url.hostname
        and url.path in ("", "/")
        and not (url.username or url.password or url.query or url.fragment),
        "Expected an explicit HTTPS port",
    )
    try:
        loopback = ipaddress.ip_address(url.hostname).is_loopback
    except ValueError:
        loopback = url.hostname == "localhost"
    require(loopback, "Fixture API must be loopback")
    require(
        set(config["users"][0]["user"])
        == {"client-certificate-data", "client-key-data"},
        "Only dedicated local kind client certificates are accepted",
    )
    require(
        b"BEGIN CERTIFICATE"
        in base64.b64decode(server["certificate-authority-data"], validate=True),
        "Local kubeconfig missing CA",
    )


def hook_resources(package):
    kinds = {
        "Job": "jobs.batch",
        "ConfigMap": "configmaps",
        "ServiceAccount": "serviceaccounts",
        "Role": "roles.rbac.authorization.k8s.io",
        "RoleBinding": "rolebindings.rbac.authorization.k8s.io",
        "ClusterRole": "clusterroles.rbac.authorization.k8s.io",
        "ClusterRoleBinding": "clusterrolebindings.rbac.authorization.k8s.io",
    }
    result = set()
    with tarfile.open(package, "r:gz") as archive:
        for member in archive.getmembers():
            if not re.fullmatch(
                r"monitoring-hook-fixture/artifacts/[0-9]{2}\.yaml", member.name
            ):
                continue
            obj = json.load(archive.extractfile(member))
            ann = obj["metadata"].get("annotations") or {}
            if "helm.sh/hook" not in ann:
                continue
            k = obj["kind"]
            require(
                k in kinds
                and set(ann["helm.sh/hook-delete-policy"].split(","))
                == {"before-hook-creation", "hook-succeeded"},
                "Unexpected hook resource or deletion policy",
            )
            scope = obj["metadata"].get("namespace", "")
            require(scope in ("", "default"), "Unexpected hook namespace")
            result.add(kinds[k] + "/" + obj["metadata"]["name"])
    require(
        len(result) == 12,
        "Expected twelve unique hook identities in the pinned fixture",
    )
    return sorted(result)


def await_empty_hooks(read_names, references, *, wait_seconds=0):
    deadline = time.monotonic() + wait_seconds
    while True:
        remaining = read_names(references).decode().strip().splitlines()
        if not remaining:
            return
        if time.monotonic() >= deadline:
            raise Failure(
                "Fixture hook resource collision or unfinished cleanup: "
                + ", ".join(remaining)
            )
        time.sleep(1)


def hook_record(secret, revision):
    require(
        secret["type"] == "helm.sh/release.v1"
        and secret["metadata"]["labels"]["name"] == "prometheus",
        "Unexpected stored Helm record",
    )
    release = json.loads(
        gzip.decompress(
            base64.b64decode(
                base64.b64decode(secret["data"]["release"], validate=True),
                validate=True,
            )
        )
    )
    require(
        release["namespace"] == "default"
        and release["name"] == "prometheus"
        and release["version"] == revision
        and release["info"]["status"] == "deployed",
        "Wrong current release or status",
    )
    jobs = {h["name"]: h for h in release["hooks"] if h["kind"] == "Job"}
    require(set(jobs) == JOBS, "Helm record differs from reviewed hook Jobs")
    out = {}
    for name, hook in jobs.items():
        run = hook["last_run"]
        require(
            run["phase"] == "Succeeded"
            and when(run["completed_at"]) >= when(run["started_at"]),
            "Helm did not observe a completed real hook Job",
        )
        out[name] = {"start": run["started_at"], "end": run["completed_at"]}
    return out


def verify_runs(current, previous, install, revision):
    if revision == 1:
        return
    fresh = JOBS if revision == 2 else {"prometheus-crds-upgrade"}
    for name in fresh:
        require(
            when(current[name]["start"]) >= when(previous[name]["end"])
            and current[name] != previous[name],
            "Expected a new successful execution for " + name,
        )
    if revision == 3:
        for name in JOBS - fresh:
            require(
                current[name] == install[name],
                "Rollback unexpectedly changed a certificate Job execution",
            )


def synthetic_secret(secret):
    return (secret["metadata"]["uid"], secret["type"], secret["data"])


def run(args, report):
    kcfg = args.kubeconfig
    local_config(kcfg, args.context)
    prefix = args.inputs.resolve()
    preflight = json.loads((prefix / "receipt.json").read_bytes())
    pkg = prefix / "package" / "monitoring-hook-fixture-0.0.1.tgz"
    seed = prefix / "seed-monitoring-crds.yaml"
    package_sha = sha(pkg.read_bytes())
    require(
        content_digest(pkg) == REVIEWED
        and package_sha == preflight["helmPackagedFixtureSHA256"]
        and sha(seed.read_bytes()) == SEED == preflight["crdPayloadSHA256"],
        "Pinned fixture input hash mismatch",
    )
    refs = hook_resources(pkg)
    report.update(
        {
            "packageSHA256": package_sha,
            "reviewedContentSHA256": REVIEWED,
            "seedCRDsSHA256": SEED,
            "hookImages": preflight["hookImages"],
            "helmCaller": "independent-local-fixture-executor",
            "productionAccess": False,
            "deploysFullMonitoringStack": False,
            "stages": [],
        }
    )
    with tempfile.TemporaryDirectory(prefix="monitoring-executor-") as home:
        env = {
            "PATH": os.defpath,
            "HOME": home,
            "KUBECONFIG": str(kcfg),
            "TMPDIR": home,
            "HELM_CONFIG_HOME": home + "/config",
            "HELM_DATA_HOME": home + "/data",
            "HELM_CACHE_HOME": home + "/cache",
            "HELM_DRIVER": "secret",
            "HELM_NO_PLUGINS": "1",
            "KUBECTL_KUBERC": "false",
            "NO_PROXY": "localhost,127.0.0.1,::1",
        }
        k = [
            str(args.kubectl),
            "--kubeconfig",
            str(kcfg),
            "--context",
            args.context,
            "--cache-dir",
            home + "/kubectl-cache",
            "--request-timeout=90s",
        ]
        h = [
            str(args.helm),
            "--kubeconfig",
            str(kcfg),
            "--kube-context",
            args.context,
            "--namespace",
            "default",
        ]
        # Copy the digest-bound archive before accepting either operation request.
        locked = Path(home) / "reviewed-chart.tgz"
        locked.write_bytes(pkg.read_bytes())
        require(
            sha(locked.read_bytes()) == package_sha
            and content_digest(locked) == REVIEWED,
            "Unexpected executor package",
        )
        pkg = locked

        def cmd(argv, *, timeout=100):
            proc = subprocess.run(
                argv, env=env, cwd=home, capture_output=True, timeout=timeout
            )
            if proc.returncode:
                raw = " ".join(proc.stderr.decode(errors="replace").split())
                safe = re.sub(r"[a-zA-Z0-9/+=_-]{48,}", "[redacted]", raw)[:520]
                raise Failure(
                    f"Local command failed ({Path(argv[0]).name}, exit {proc.returncode}): {safe}"
                )
            return proc.stdout

        def kj(argv):
            return json.loads(cmd(k + argv + ["-o", "json"]))

        def get(obj, name):
            return kj(["-n", "default", "get", obj, name])

        def stage(label):
            report["currentStage"] = label
            print(label, flush=True)

        def read_crds():
            docs = kj(["get", "-f", str(seed)])["items"]
            require(
                sorted(o["metadata"]["name"] for o in docs) == preflight["crds"],
                "CRD names differ",
            )
            for d in docs:
                require(
                    any(
                        c["type"] == "Established" and c["status"] == "True"
                        for c in d["status"]["conditions"]
                    ),
                    "CRD is not Established",
                )
            return {o["metadata"]["name"]: o["metadata"]["uid"] for o in docs}

        def webhook_state():
            secret = get("secret", PREFIX)
            require(
                {"ca", "cert", "key"} <= set(secret["data"]),
                "Admission Secret missing fixture key material",
            )
            ca = secret["data"]["ca"]
            require(
                b"BEGIN CERTIFICATE" in base64.b64decode(ca, validate=True),
                "Admission CA invalid",
            )
            wh = []
            for kind in (
                "mutatingwebhookconfiguration",
                "validatingwebhookconfiguration",
            ):
                obj = get(kind, PREFIX)
                for item in obj["webhooks"]:
                    client = item["clientConfig"]
                    require(
                        client.get("caBundle") == ca
                        and client.get("service", {}).get("namespace") == "default"
                        and client["service"]["name"]
                        == "prometheus-kube-prometheus-operator",
                        "Webhook CA or target mismatch",
                    )
                    wh.append(item["name"])
            require(len(wh) == 3, "Expected three webhook entries")
            return secret["metadata"]["uid"], secret["data"]

        def job_events(known, expected):
            # Events are independent evidence and asynchronous. Helm records below are decisive.
            rows = {}
            for _ in range(4):
                rows = {}
                events = kj(
                    [
                        "-n",
                        "default",
                        "get",
                        "events",
                        "--field-selector",
                        "involvedObject.kind=Job",
                    ]
                )["items"]
                for ev in events:
                    ref = ev.get("involvedObject", {})
                    n = ref.get("name")
                    uid = ref.get("uid")
                    if n in expected and uid and uid not in known:
                        rows.setdefault(n, {}).setdefault(uid, set()).add(
                            ev.get("reason", "")
                        )
                if all(
                    any("Completed" in reasons for reasons in rows.get(n, {}).values())
                    for n in expected
                ):
                    break
                time.sleep(1)
            # Suppress private local UIDs and raw event messages from receipt.
            summaries = {
                n: sorted(
                    {
                        x
                        for reasons in rows.get(n, {}).values()
                        for x in reasons
                        if x in {"Completed", "SuccessfulCreate", "SuccessCriteriaMet"}
                    }
                )
                for n in sorted(expected)
            }
            known.update(uid for scopes in rows.values() for uid in scopes)
            return summaries

        stage("verify clean local cluster and client")
        hv = cmd(h + ["version", "--short"]).decode().strip()
        require(hv.startswith("v4.3.2+"), "Helm 4.3.2 required")
        sv = kj(["version"])["serverVersion"]["gitVersion"]
        require(sv.startswith("v1.36."), "Fixture requires Kubernetes 1.36.x")
        nodes = kj(["get", "nodes"])["items"]
        require(
            [n["metadata"]["name"] for n in nodes] == [NODE],
            "Unexpected local node inventory",
        )
        require(
            not any(
                t.get("effect") == "NoSchedule"
                for t in nodes[0]["spec"].get("taints", [])
            ),
            "Node cannot schedule real hook Pods",
        )
        require(
            any(
                c["type"] == "Ready" and c["status"] == "True"
                for c in nodes[0]["status"]["conditions"]
            ),
            "Node is not Ready",
        )
        report.update(
            helm=hv,
            kubernetes=sv,
            nodeArchitecture=nodes[0]["status"]["nodeInfo"]["architecture"],
        )
        require(
            not json.loads(cmd(h + ["list", "--filter", "^prometheus$", "-o", "json"])),
            "Fixture has an existing release",
        )
        require(
            not any(
                o["metadata"]["name"].endswith("monitoring.coreos.com")
                for o in kj(["get", "crds"])["items"]
            ),
            "Fixture has pre-existing monitoring CRDs",
        )
        for name in SECRETS | {PREFIX}:
            require(
                not cmd(
                    k
                    + [
                        "-n",
                        "default",
                        "get",
                        "secret",
                        name,
                        "--ignore-not-found",
                        "-o",
                        "name",
                    ]
                ).strip(),
                "Previously existing fixture Secret",
            )
        for kind in ("mutatingwebhookconfiguration", "validatingwebhookconfiguration"):
            require(
                not cmd(
                    k + ["get", kind, PREFIX, "--ignore-not-found", "-o", "name"]
                ).strip(),
                "Previously existing monitoring webhook",
            )

        def hook_names(refs):
            return cmd(
                k
                + ["-n", "default", "get"]
                + refs
                + ["--ignore-not-found", "-o", "name"]
            )

        await_empty_hooks(hook_names, refs)
        report["initiallyAbsentHookResources"] = len(refs)
        stage("seed exact ten monitoring CRDs on disposable cluster")
        cmd(
            k
            + [
                "apply",
                "--server-side",
                "--field-manager=fixture-seed",
                "-f",
                str(seed),
            ]
        )
        cmd(
            k
            + ["wait", "--for=condition=Established", "--timeout=2m", "-f", str(seed)],
            timeout=150,
        )
        crd_baseline = read_crds()
        gate = TriggerFixture(k, kcfg, home, package_sha, env)
        report["requester"] = gate.evidence
        baseline = None
        initial_admission = None
        known = set()
        runs = []
        for revision, label, command in [
            (
                1,
                "install",
                [
                    "install",
                    "prometheus",
                    str(pkg),
                    "--set-string",
                    "fixtureMarker=baseline",
                ],
            ),
            (
                2,
                "upgrade",
                [
                    "upgrade",
                    "prometheus",
                    str(pkg),
                    "--set-string",
                    "fixtureMarker=candidate",
                ],
            ),
            (3, "rollback", ["rollback", "prometheus", "1"]),
        ]:
            stage("execute fixture " + label)
            if revision != 1:
                gate.claim(
                    label,
                    revision,
                    json.loads(cmd(h + ["history", "prometheus", "-o", "json"])),
                )
            cmd(h + command + ["--wait", "--timeout", "20m0s"], timeout=1800)
            stage("verify fixture " + label)
            await_empty_hooks(hook_names, refs, wait_seconds=20)
            current = hook_record(
                get("secret", f"sh.helm.release.v1.prometheus.v{revision}"), revision
            )
            verify_runs(
                current, runs[-1] if runs else {}, runs[0] if runs else {}, revision
            )
            runs.append(current)
            objects = {name: get("secret", name) for name in SECRETS}
            marker = "candidate" if revision == 2 else "baseline"
            require(
                objects["prometheus-grafana"]["metadata"]["annotations"].get(MARKER)
                == marker,
                "Fixture Grafana marker did not transition",
            )
            require(
                all(
                    MARKER not in (v["metadata"].get("annotations") or {})
                    for n, v in objects.items()
                    if n != "prometheus-grafana"
                ),
                "Marker escaped Grafana Secret",
            )
            full = {name: synthetic_secret(o) for name, o in objects.items()}
            if revision == 1:
                baseline = full
            require(
                full == baseline, "Synthetic Secret identity, type or payload changed"
            )
            require(read_crds() == crd_baseline, "Established CRD UIDs changed")
            cert = webhook_state()
            if revision == 1:
                initial_admission = cert
            require(
                initial_admission == cert,
                "Fixture admission certificate changed unexpectedly",
            )
            fresh = JOBS if revision < 3 else {"prometheus-crds-upgrade"}
            report["stages"].append(
                {
                    "action": label,
                    "revision": revision,
                    "helmStatus": "deployed",
                    "newlyCompletedHookJobs": sorted(fresh),
                    "absentHookResourcesAfterTransaction": len(refs),
                    "hookRuns": {n: current[n] for n in sorted(fresh)},
                    "jobControllerEvents": job_events(known, fresh),
                    "matchingEstablishedCRDs": 10,
                    "unchangedApplicationSecretPayloadsAndUIDs": 3,
                    "grafanaFixtureMarker": marker,
                    "webhookCABundlesMatchAdmissionSecret": 3,
                    "admissionSecretUIDAndDataUnchanged": True,
                }
            )
        hist = json.loads(cmd(h + ["history", "prometheus", "-o", "json"]))
        states = [(int(x["revision"]), x["status"]) for x in hist]
        require(
            states == [(1, "superseded"), (2, "superseded"), (3, "deployed")],
            "Unexpected final Helm history",
        )
        report["history"] = [{"revision": v, "status": s} for v, s in states]
        report["limits"] = [
            "Fixture after local admin seeded existing CRDs. Admission webhook serving and full chart are outside this test.",
            "The independent fixture executor uses local administrator credentials. The source of a production executor, its least-privilege permissions and the full chart are separate.",
            "All three transactions used the same reviewed pinned chart with a synthetic marker; real production rollback inputs differ.",
        ]
        report["passed"] = True


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--kubeconfig", type=Path, required=True)
    p.add_argument("--context", required=True)
    p.add_argument("--receipt", type=Path, required=True)
    p.add_argument("--helm", type=Path, required=True)
    p.add_argument("--kubectl", type=Path, required=True)
    p.add_argument("--inputs", required=True, type=Path)
    args = p.parse_args()
    os.umask(0o077)
    if args.receipt.exists():
        raise SystemExit("New receipt path required")
    report = {
        "startedAt": utc(),
        "passed": False,
        "driverSHA256": sha(Path(__file__).read_bytes()),
    }
    try:
        run(args, report)
    except Exception as exc:
        report["failure"] = (
            str(exc)[:620]
            if isinstance(exc, (Failure, RuntimeError))
            else type(exc).__name__
        )
    report["finishedAt"] = utc()
    with args.receipt.open("x") as out:
        json.dump(report, out, indent=2)
        out.write("\n")
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "failure": report.get("failure"),
                "receiptSHA256": sha(args.receipt.read_bytes()),
            }
        )
    )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
