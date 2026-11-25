#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Rehearse the Helmfile adoption runbook of docs/cutover.md on disposable Kubernetes.

The runbook's bash blocks marked `<!-- rehearse: <id> -->` are read from docs/cutover.md when
this runs (scripts/demo/runbook_blocks.py) and their text runs verbatim with `bash` from the
repository root. Each is a `bash -euo pipefail <<'EOF'` script, as an operator pastes it. The
runbook's variables are added to the job's environment, with umask 077, as its session blocks
set them. The operator shell block writes the deployer kubeconfig with
scripts/demo/deployer_kubeconfig.py, impersonating a fixture manual user bound to the committed
roles of ops/rbac/deployer.yaml, and every later block runs through it; the blocks the runbook
marks as the administrator's pass the kind administrator kubeconfig themselves.

The fixture is the Helmfile engine rehearsal's (rehearsals/helmfile-deploy): six synthetic
application releases under the real names, in the four real namespaces, plus the real
demo-resources chart and sources, fixture Jaeger and HotROD Deployments with real snapshot
images, Certificates, and here an OpenSearch StatefulSet with a bound claim that the
opensearch release owns. A workflow dispatch is replaced by helmfile_deploy.reconcile with the
environment the dispatch gives the Reconcile step; the artifact download by a copy of the
record it wrote. The registry lookup, the public health checks and the trace check are stubs.

The walk: the operator shell and the read-only checks; for each namespace the ownership patches
and the first companion install, with one failed install recovered and retried; the adoption
apply and a second no-op apply with unchanged identities; a failed release refused in live mode
and recovered by rollback; a pending release refused and recovered; a rollback to a revision
recorded by a digest-mode apply, its two refusals, and the next apply that undoes it; an
immutable StatefulSet field repaired by an orphan delete and a sync that keeps the Pod and the
claim; and de-adoption of every namespace.
"""

import argparse
import base64
import gzip
import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2]))
import companion_values  # noqa: E402
import deploy  # noqa: E402
import deployer_access  # noqa: E402
import helmfile_deploy  # noqa: E402
import release_phases  # noqa: E402
import runbook_blocks  # noqa: E402


def rehearsal(name):
    """Load another rehearsal's module for its fixture builders; its directory name has a hyphen."""
    spec = importlib.util.spec_from_file_location(name.replace("-", "_") + "_rehearsal",
                                                  HERE.parents[1] / name / "exercise.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixture = rehearsal("helmfile-deploy")

CONTEXT = "kind-jaeger-demo-runbook-fixture"
# Fixture identities in the OCI user OCID form; bound to the committed roles like the production ones.
MANUAL = "ocid1.user.oc1..aaaarehearsalrunbookmanual"
SCHEDULED = "ocid1.user.oc1..aaaarehearsalrunbookscheduled"
REPO = runbook_blocks.ROOT
# The runbook's blocks that this walk runs, by their rehearse id. runbook_blocks_test.py requires
# that every rehearse id of docs/cutover.md is here and nothing else is.
BLOCKS = (
    "operator-kubeconfig",
    "impersonated-access",
    "releases-before",
    "access-check",
    "certificates",
    "certificates-default",
    "capture-identities",
    "compare-identities",
    "ownership-plan",
    "ownership-apply",
    "install-check",
    "adoption-record",
    "releases-after",
    "release-diagnostics",
    "release-events",
    "recovery-revision",
    "recorded-revisions",
    "rollback",
    "statefulset-diagnose",
    "statefulset-claim",
    "statefulset-selector",
    "statefulset-orphan",
    "statefulset-verify",
    "deadopt-records",
    "deadopt-delete",
    "deadopt-plan",
    "deadopt-apply",
    "deadopt-compare",
)
# The variables the runbook documents. A block gets these added to the job's own environment.
DOCUMENTED = runbook_blocks.VARIABLES
# The runbook's adoption order.
ORDER = ("default", "otel-demo", "opensearch", "jaeger")
APPLICATIONS = fixture.APPLICATIONS
COMPANIONS = [ns + "/" + release_phases.COMPANION for ns in release_phases.NAMESPACES]
# The opensearch release of the fixture chart owns the StatefulSet, as the real chart does. Its
# podManagementPolicy is immutable; the repair step changes it.
STATEFULSET = """{{- if eq .Release.Name "opensearch" }}
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: opensearch-cluster-single
spec:
  serviceName: opensearch-cluster-single
  replicas: 1
  podManagementPolicy: OrderedReady
  selector:
    matchLabels: {app: opensearch-cluster-single-fixture}
  template:
    metadata:
      labels: {app: opensearch-cluster-single-fixture}
    spec:
      containers:
        - name: opensearch
          image: registry.k8s.io/pause:3.10
          volumeMounts:
            - {name: opensearch-cluster-single, mountPath: /usr/share/opensearch/data}
  volumeClaimTemplates:
    - metadata:
        name: opensearch-cluster-single
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests: {storage: 1Mi}
{{- end }}
"""


def release_record(secret):
    return json.loads(gzip.decompress(base64.b64decode(base64.b64decode(secret["data"]["release"]))))


def encoded(payload):
    return base64.b64encode(base64.b64encode(gzip.compress(json.dumps(payload).encode()))).decode()


def exercise(cfg, context):
    ctl = ["kubectl", "--kubeconfig", str(cfg), "--context", context]
    real_helm = shutil.which("helm")
    admin_helm = [real_helm, "--kubeconfig", str(cfg), "--kube-context", context]
    view = json.loads(subprocess.check_output(ctl + ["config", "view", "--minify", "-o", "json"]))
    if urlsplit(view["clusters"][0]["cluster"]["server"]).hostname != "127.0.0.1":
        raise ValueError("A disposable loopback cluster is required")
    present = set(subprocess.check_output(ctl + ["get", "ns", "-o", "jsonpath={.items[*].metadata.name}"], text=True).split())
    if present & set(release_phases.NAMESPACES[1:]):
        raise ValueError("Use a new fixture without the release namespaces")
    text = runbook_blocks.DOC.read_text()
    disagreements = runbook_blocks.problems(text, BLOCKS)
    if disagreements:
        raise AssertionError("The runbook and this rehearsal disagree:\n" + "\n".join(disagreements))

    def admin(*args, data=None):
        return subprocess.run(ctl + list(args), input=None if data is None else json.dumps(data).encode(),
                              check=True, stdout=subprocess.DEVNULL)

    def admin_json(*args):
        return json.loads(subprocess.check_output(ctl + list(args) + ["-o", "json"]))

    # ------------------------------------------------------------ the fixture, as the administrator
    for ns in release_phases.NAMESPACES[1:]:
        admin("create", "namespace", ns)
    admin("apply", "-f", str(deployer_access.ROLES))
    admin("apply", "-f", "-", data=deployer_access.bindings(MANUAL, SCHEDULED))
    admin("-n", "default", "create", "configmap", deploy.STATE,
          *[f"--from-literal={name}=stale" for name in deploy.COMPONENTS])
    admin("apply", "-f", "-", data=fixture.CERTIFICATE_CRD)
    admin("wait", "--for", "condition=established", "--timeout=60s", "crd/certificates.cert-manager.io")
    workloads = [item for item in fixture.workloads()["items"] if item["kind"] != "StatefulSet"]
    admin("apply", "-f", "-", data={"apiVersion": "v1", "kind": "List", "items": workloads})
    for item in workloads:
        admin("-n", item["metadata"]["namespace"], "rollout", "status",
              f"{item['kind'].lower()}/{item['metadata']['name']}", "--timeout=5m")

    scratch = tempfile.TemporaryDirectory()
    work = Path(scratch.name)
    root, _ = fixture.fixture_root(work, mode="live")
    # The engine writes its record and work files into the configuration root; keep them out of commits.
    (root / ".git/info/exclude").write_text(".tools/\ndeploy-record.json\n")
    template = root / "fixture-charts/app/templates/statefulset.yaml"
    template.write_text(STATEFULSET)
    head = fixture.commit(root, "fixture StatefulSet")
    # The OTel Certificates the engine checks, and the OCI one only the administrator can read.
    for ns, name in helmfile_deploy.expected_certificates(root) + [("default", "demo-jaeger-tls")]:
        admin("apply", "-f", "-", data=fixture.certificate(ns, name))

    # The six applications as production holds them before adoption: installed by an earlier writer
    # with exactly the live image references (the values the engine reads in live mode).
    live = {stack: {name: {"repository": repo, "tag": fixture.LIVE_TAG, "pullPolicy": "IfNotPresent"}
                    for name, repo in (("jaeger", fixture.LIVE_REPOS["jaeger"]),
                                       ("hotrod", fixture.LIVE_REPOS["jaeger-hotrod"]))}
            for stack in ("oci", "otel")}
    values = work / "seed-values.json"
    values.write_text(json.dumps({"images": live}))
    seed = ["helmfile", "-f", str(root / helmfile_deploy.APPLICATIONS), "--state-values-file", str(values), "--skip-deps"]
    admin_env = dict(os.environ, KUBECONFIG=str(cfg))
    subprocess.run(seed + ["sync", "--concurrency", "1"], cwd=root, env=admin_env, check=True, stdout=subprocess.DEVNULL)
    # As in the engine rehearsal: one more upgrade settles the monitoring fixture's looked-up Secret.
    subprocess.run(seed + ["-l", "namespace=default,name=prometheus", "sync"], cwd=root, env=admin_env, check=True,
                   stdout=subprocess.DEVNULL)

    # The supplemental objects as the scripts apply them: kubectl-managed, with no Helm ownership,
    # and the trace generators restarted afterwards.
    sources = companion_values.source_values()["companions"]
    for ns in release_phases.NAMESPACES:
        for obj in sources[ns]["objects"]:
            admin("-n", ns, "apply", "-f", "-", data=obj)
    for ns in ("default", "jaeger"):
        admin("-n", ns, "rollout", "restart", "deployment/trace-generator")
        admin("-n", ns, "rollout", "status", "deployment/trace-generator", "--timeout=5m")

    def supplemental():
        """Labels and annotations of the allowlisted objects, read by the administrator."""
        found = {}
        for ns in release_phases.NAMESPACES:
            for obj in sources[ns]["objects"]:
                live_obj = admin_json("-n", ns, "get", helmfile_deploy.KINDS[obj["kind"]], obj["metadata"]["name"])
                meta = live_obj["metadata"]
                annotations = dict(meta.get("annotations") or {})
                # The Deployment controller counts rollouts here; digest-mode applies restart the generators.
                annotations.pop("deployment.kubernetes.io/revision", None)
                found[ns, obj["kind"], meta["name"]] = (meta["uid"], meta.get("labels") or {}, annotations)
        return found

    unadopted = supplemental()

    # ------------------------------------------------------------ the runbook's blocks and dispatches
    record_dir = work / "record"
    deployer = record_dir / "deployer.kubeconfig"
    # What the runbook's session blocks set in the operator's shell.
    session = {"REVIEWED_COMMIT": head, "ADMIN_KUBECONFIG": str(cfg), "ADMIN_CONTEXT": context,
               "MANUAL_USER_OCID": MANUAL, "SCHEDULED_USER_OCID": SCHEDULED, "RECORD": str(record_dir),
               "KUBECONFIG": str(deployer), "CONTEXT": context}
    os.umask(0o077)
    ran = []

    def block(key, expect_failure=False, **variables):
        """Run one block of the runbook, read from docs/cutover.md now, verbatim. Returns the result."""
        assert key in BLOCKS, key
        assert set(variables) <= DOCUMENTED, set(variables) - DOCUMENTED
        code = runbook_blocks.rehearsed()[key]
        assert runbook_blocks.strict_body(code) is not None, f"{key} is not a strict script"
        env = {name: value for name, value in os.environ.items() if name not in DOCUMENTED | {"PYTHONPATH"}}
        env.update(session)
        env.update(variables)
        # The block's own text, as an operator pastes it into a shell without -e or pipefail.
        result = subprocess.run(["bash", "-c", code], cwd=REPO, env=env, capture_output=True, text=True)
        label = " ".join(f"{name}={value}" for name, value in sorted(variables.items()))
        print(f"::group::runbook block {key} {label}".rstrip(), flush=True)
        print(result.stdout, end="", flush=True)
        print(result.stderr, end="", file=sys.stderr, flush=True)
        print("::endgroup::", flush=True)
        if bool(result.returncode) != expect_failure:
            raise AssertionError(f"Runbook block {key} ({label}) exited {result.returncode}")
        ran.append(key)
        return result

    commands = []

    def run(args, **kwargs):
        commands.append([str(a) for a in args])
        return helmfile_deploy.run_command(args, **kwargs)

    def writes():
        return [c for c in commands if (c[0] == "helmfile" and c[c.index("--skip-deps") + 1] in ("apply", "sync"))
                or c[:2] == ["helm", "install"] or (c[0] == "kubectl" and ({"patch", "restart"} & set(c)))]

    def charts(path):
        return {"charts": {"app-fixture": "rehearsal"}}

    def resolve_image(repo, tag, reference_digest=None):
        # The registry stand-in: the digest the node pulled for that reference.
        pods = admin_json("get", "pods", "-A")["items"]
        found = {status["imageID"].split("@")[-1] for pod in pods for status in pod["status"].get("containerStatuses", [])
                 if status["image"] in (f"docker.io/{repo}:{tag}", f"{repo}:{tag}")}
        digest, = found
        return {"repository": repo, "tag": tag, "sourceCommit": tag, "digest": digest, "platformManifest": digest}

    def live_images(r):
        return helmfile_deploy.read_live_images(r, resolve_image=resolve_image)

    summary = work / "summary.md"
    state = {"head": head}

    def base(run_id):
        return {"GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main", "GITHUB_SHA": state["head"],
                "EXPECTED_CONFIG_SHA": state["head"], "DEPLOY_ENGINE": "helmfile", "HELMFILE_OPERATION": "apply",
                "COMPANION_NAMESPACE": "none", "GITHUB_RUN_ID": run_id, "GITHUB_STEP_SUMMARY": str(summary),
                "MAIN_SHA": "0" * 40}

    def dispatch(run_id, **env):
        """The workflow dispatch: what its Reconcile step runs, then the record download."""
        commands.clear()
        mode = helmfile_deploy.image_mode(root)
        checks = (helmfile_deploy.ProductionChecks(root, run, health=lambda stack: {"stack": stack, "stub": True},
                                                   trace=lambda *versions: None, sleep=lambda s: None)
                  if mode == "live" else fixture.Checks(admin_helm))
        error = None
        try:
            helmfile_deploy.reconcile(dict(base(run_id), **env), root=root, run=run, checks=checks, verify_charts=charts,
                                      live_images=live_images, sleep=lambda s: None)
        except (ValueError, RuntimeError, subprocess.CalledProcessError) as caught:
            error = caught
        target = record_dir / "runs" / run_id
        target.mkdir(parents=True)
        shutil.copy(root / helmfile_deploy.RECORD, target / helmfile_deploy.RECORD)
        return json.loads((target / helmfile_deploy.RECORD).read_text()), error

    def digest_images(tag):
        """The workflow's Resolve and validate images step in digest mode, with the stub registry and gate."""
        def resolve(event, overrides, core_main=None):
            return fixture.selection(state["head"], tag)

        return helmfile_deploy.images(base("images"), root=root, run=run, resolve=resolve, validate=fixture.receipt)

    def history(key):
        ns, name = key.split("/")
        out = subprocess.run(admin_helm + ["-n", ns, "history", name, "-o", "json"], capture_output=True, text=True)
        return [] if out.returncode else [(int(e["revision"]), e["status"]) for e in json.loads(out.stdout)]

    def latest():
        return {key: (history(key) or [None])[-1] for key in APPLICATIONS + COMPANIONS}

    def record_secret(key, revision):
        ns, name = key.split("/")
        return admin_json("-n", ns, "get", "secret", f"sh.helm.release.v1.{name}.v{revision}")

    def interrupted(key, status, app_version=None):
        """What a transaction that never recorded its end leaves: a copy of the latest record, one higher.

        Only the release records change, as in the engine rehearsal. With app_version the copy also
        names another application version. Returns the revision it copied.
        """
        ns, name = key.split("/")
        revision = history(key)[-1][0]
        secret = record_secret(key, revision)
        payload = release_record(secret)
        payload["version"] = revision + 1
        payload["info"]["status"] = status
        payload["info"]["description"] = f"Rehearsal: a transaction interrupted in {status}"
        if app_version:
            payload["chart"]["metadata"]["appVersion"] = app_version
        labels = dict(secret["metadata"]["labels"], status=status, version=str(revision + 1))
        admin("create", "-f", "-", data={"apiVersion": "v1", "kind": "Secret", "type": secret["type"],
                                         "metadata": {"name": f"sh.helm.release.v1.{name}.v{revision + 1}",
                                                      "namespace": ns, "labels": labels},
                                         "data": {"release": encoded(payload)}})
        assert history(key)[-2:] == [(revision, "deployed"), (revision + 1, status)], history(key)
        return revision

    def mark(key, status):
        """Set the status of the latest record in place, as a transaction that ended so leaves it."""
        ns, name = key.split("/")
        secret = record_secret(key, history(key)[-1][0])
        payload = release_record(secret)
        payload["info"]["status"] = status
        secret["data"]["release"] = encoded(payload)
        secret["metadata"]["labels"]["status"] = status
        admin("-n", ns, "replace", "-f", "-", data=secret)

    # 1. Operator shell. The session blocks' variables and umask are set above.
    out = block("operator-kubeconfig").stdout
    assert "act as the manual deployer identity" in out, out
    assert stat.S_IMODE(record_dir.stat().st_mode) == 0o700 and stat.S_IMODE(deployer.stat().st_mode) == 0o600
    assert MANUAL not in out, "The deployer identity's OCID is printed only by kubectl, and not here"
    # Started again, the block keeps the kubeconfig it wrote.
    written = deployer.read_bytes()
    block("operator-kubeconfig")
    assert deployer.read_bytes() == written
    # The in-process engine calls, like the workflow's, use the same deployer kubeconfig.
    os.environ["KUBECONFIG"] = str(deployer)
    assert "manual: 0 difference(s)" in block("impersonated-access").stdout

    # 2. Read-only checks before adoption.
    block("releases-before")
    assert len(json.loads((record_dir / "releases-before.json").read_text())) == 6
    assert all((record_dir / f"history-before-{key.replace('/', '-')}.json").is_file() for key in APPLICATIONS)
    out = block("access-check").stdout
    assert "manual: 0 difference(s)" in out and "scheduled: 0 difference(s)" in out, out
    out = block("certificates").stdout
    assert len(out.strip().splitlines()) == 3 and all(line.endswith("\tTrue") for line in out.strip().splitlines()), out
    assert "default\tdemo-jaeger-tls\tTrue" in block("certificates-default").stdout
    # The administrator's read fails on a Certificate that is not Ready, as the deployer's does.
    admin("apply", "-f", "-", data=fixture.certificate("default", "demo-jaeger-tls", ready="False"))
    block("certificates-default", expect_failure=True)
    admin("apply", "-f", "-", data=fixture.certificate("default", "demo-jaeger-tls"))
    block("capture-identities", STEP="baseline")
    baseline = json.loads((record_dir / "identities-baseline.json").read_text())
    kinds = {entry["kind"] for entry in baseline}
    assert {"Deployment", "DaemonSet", "StatefulSet", "Service", "ReplicaSet", "PersistentVolumeClaim"} <= kinds, kinds
    claim, = [entry for entry in baseline if entry["kind"] == "PersistentVolumeClaim"]
    # A block refuses to run without a variable it needs, before any command.
    assert "STEP" in block("capture-identities", expect_failure=True, STEP="").stderr
    # The live-mode diff of the runbook, on this fixture's state: no application change, and the
    # companions refused because they are not installed yet.
    planned = helmfile_deploy.diff(base("diff"), root=root, run=run, live_images=live_images, verify_charts=charts)
    assert planned == {key: "error" if key in COMPANIONS else "no changes" for key in APPLICATIONS + COMPANIONS}, planned
    assert not writes()

    # 3 and 4. Ownership patches and the first companion install, one namespace at a time.
    for ns in ORDER:
        block("ownership-plan", ns=ns)
        plan = json.loads((record_dir / f"ownership-{ns}.json").read_text())
        assert plan and all(item["patch"] for item in plan), plan
        block("ownership-apply", ns=ns)
        record, error = dispatch(f"install-{ns}", HELMFILE_OPERATION="install-companion", COMPANION_NAMESPACE=ns)
        assert error is None, error
        install, = [c for c in commands if c[:2] == ["helm", "install"]]
        assert "--take-ownership" not in install and not [c for c in commands if "restart" in c]
        block("install-check", ns=ns, RUN_ID=f"install-{ns}", REVIEWED_COMMIT=state["head"])
        # The check is bound to the run and the commit it names.
        block("install-check", expect_failure=True, ns=ns, RUN_ID=f"install-{ns}", REVIEWED_COMMIT="f" * 40)
        if ns == "opensearch":
            # A first install that failed: the check fails, de-adoption step 1 removes only the
            # release record, and the install is dispatched again with the ownership metadata kept.
            mark(f"{ns}/{release_phases.COMPANION}", "failed")
            block("install-check", expect_failure=True, ns=ns, RUN_ID=f"install-{ns}")
            assert f"sh.helm.release.v1.{release_phases.COMPANION}.v1" in block("deadopt-records", ns=ns).stdout
            assert "release: not found" in block("deadopt-delete", ns=ns).stdout
            record, error = dispatch(f"install-{ns}-again", HELMFILE_OPERATION="install-companion", COMPANION_NAMESPACE=ns)
            assert error is None, error
            block("install-check", ns=ns, RUN_ID=f"install-{ns}-again")
        block("capture-identities", STEP=f"companion-{ns}")
        block("compare-identities", STEP=f"companion-{ns}")
    adopted = json.loads((record_dir / "adopted.json").read_text())
    assert len(adopted) == sum(len(sources[ns]["objects"]) for ns in ORDER) == 12, adopted

    # 5. The adoption apply and the no-op, both in live mode.
    for run_id in ("adoption", "no-op"):
        record, error = dispatch(run_id)
        assert error is None, error
        assert not [c for c in commands if "restart" in c], "A live-mode apply restarted a generator"
        block("adoption-record", RUN_ID=run_id)
        block("capture-identities", STEP=run_id)
        block("compare-identities", STEP=run_id)
    block("adoption-record", expect_failure=True, RUN_ID="no-op", REVIEWED_COMMIT="f" * 40)
    assert {key: entry[1] for key, entry in latest().items()} == dict.fromkeys(APPLICATIONS + COMPANIONS, "deployed")
    block("releases-after")
    adopted_revisions = latest()

    # 6. A failed release, in live mode: the engine refuses it, and a rollback to the last deployed
    # revision recovers it.
    failed = interrupted("otel-demo/otel-demo", "failed")
    record, error = dispatch("failed")
    assert isinstance(error, ValueError) and "live-diff" in str(error), error
    assert record["adoptionGate"] == {"gate": "live-diff", "release": "otel-demo/otel-demo", "result": "changes"}, record
    assert not writes()
    assert "failed" in block("release-diagnostics", ns="otel-demo", release="otel-demo").stdout
    block("release-events", ns="otel-demo")
    block("release-events", ns="default")
    revision = block("recovery-revision", ns="otel-demo", release="otel-demo").stdout.strip()
    assert revision == str(failed), revision
    out = block("rollback", ns="otel-demo", release="otel-demo", REVISION=revision).stdout
    assert f"Revision {revision} is deployed again" in out, out
    after = history("otel-demo/otel-demo")
    assert after[-1] == (failed + 2, "deployed") and "deployed" not in [s for _, s in after[:-1]], after
    record, error = dispatch("after-failed")
    assert error is None, error
    block("adoption-record", RUN_ID="after-failed")
    # The guard: with the release deployed again there is nothing to recover.
    block("recovery-revision", expect_failure=True, ns="otel-demo", release="otel-demo")

    # 7. A pending release: refused before any write, then recovered once no writer holds it.
    pending = interrupted("jaeger/jaeger", "pending-upgrade")
    record, error = dispatch("pending")
    assert isinstance(error, ValueError) and "operator review" in str(error), error
    assert record["outcome"] == "refused" and not writes()
    assert "pending-upgrade" in block("release-diagnostics", ns="jaeger", release="jaeger").stdout
    revision = block("recovery-revision", ns="jaeger", release="jaeger").stdout.strip()
    assert revision == str(pending), revision
    block("rollback", ns="jaeger", release="jaeger", REVISION=revision)
    after = history("jaeger/jaeger")
    assert after[-1] == (pending + 2, "deployed") and "deployed" not in [s for _, s in after[:-1]], after
    record, error = dispatch("after-pending")
    assert error is None, error
    block("adoption-record", RUN_ID="after-pending")
    block("capture-identities", STEP="after-recovery")
    block("compare-identities", STEP="after-recovery")

    # 8. Rollback refusals: a revision that is not in the history, and in opensearch a target with
    # another application version, which would be a downgrade. Neither writes a revision.
    held = history("opensearch/opensearch")
    result = block("rollback", expect_failure=True, ns="opensearch", release="opensearch", REVISION="99")
    assert "Stop: revision 99" in result.stderr, result.stderr
    upgraded = interrupted("opensearch/opensearch", "deployed", app_version="rehearsal-2")
    result = block("rollback", expect_failure=True, ns="opensearch", release="opensearch", REVISION=str(upgraded))
    assert "OpenSearch version" in result.stderr, result.stderr
    admin("-n", "opensearch", "delete", "secret", f"sh.helm.release.v1.opensearch.v{upgraded + 1}")
    assert history("opensearch/opensearch") == held, history("opensearch/opensearch")

    # A rollback to a revision a digest-mode apply recorded, and the next apply that undoes it.
    (root / "helmfile/images.yaml").write_text("mode: digest\n")
    state["head"] = session["REVIEWED_COMMIT"] = fixture.commit(root, "digest mode")
    digest_images("2" * 40)
    record, error = dispatch("digest")
    assert error is None, error
    changed = {r["id"]: r["revisionBefore"] for r in record["releases"] if r["changed"]}
    assert set(changed) == {"default/jaeger", "jaeger/jaeger"}, changed
    out = block("recorded-revisions", RUN_ID="digest").stdout
    listed = [line.split("\t") for line in out.strip().splitlines()]
    assert {(key, int(before)) for key, before, _, _ in listed} == set(changed.items()), listed
    for key, before, _, _ in reversed(listed):
        ns, name = key.split("/")
        block("rollback", ns=ns, release=name, REVISION=before)
    record, error = dispatch("digest-again")
    assert error is None, error
    undone = {r["id"] for r in record["releases"] if r["changed"]}
    assert undone == set(changed), f"The next apply must re-apply main to the rolled-back releases: {undone}"

    # 9. An immutable StatefulSet field: the upgrade fails, the object is deleted with orphan
    # propagation, and the next apply syncs the failed release, which creates it again on the same
    # claim and adopts the same Pod.
    before = admin_json("-n", "opensearch", "get", "statefulset", "opensearch-cluster-single")
    pod_before = admin_json("-n", "opensearch", "get", "pod", "opensearch-cluster-single-0")
    template.write_text(STATEFULSET.replace("podManagementPolicy: OrderedReady", "podManagementPolicy: Parallel"))
    state["head"] = session["REVIEWED_COMMIT"] = fixture.commit(root, "an immutable StatefulSet change")
    digest_images("2" * 40)
    record, error = dispatch("statefulset")
    assert error is not None and record["outcome"] == "failed", record
    assert record["error"]["release"] == "opensearch/opensearch", record["error"]
    assert history("opensearch/opensearch")[-1][1] == "failed"
    out = block("statefulset-diagnose").stdout.lower()
    # Kubernetes 1.37 names the field as immutable; 1.33 to 1.36 report one Forbidden error.
    assert "failed" in out and ("field is immutable" in out or "forbidden" in out), out
    block("statefulset-claim")
    assert "The new selector matches the running Pod" in block("statefulset-selector").stdout
    block("statefulset-orphan")
    pod = admin_json("-n", "opensearch", "get", "pod", "opensearch-cluster-single-0")
    assert not pod["metadata"].get("ownerReferences"), pod["metadata"].get("ownerReferences")
    gone = subprocess.run(ctl + ["-n", "opensearch", "get", "statefulset", "opensearch-cluster-single"],
                          capture_output=True, text=True)
    assert gone.returncode and "NotFound" in gone.stderr, gone.stderr
    record, error = dispatch("statefulset-repair")
    assert error is None, error
    opensearch, = [r for r in record["releases"] if r["id"] == "opensearch/opensearch"]
    assert (opensearch["statusBefore"], opensearch["method"], opensearch["statusAfter"]) == ("failed", "sync", "deployed")
    block("statefulset-verify")
    after = admin_json("-n", "opensearch", "get", "statefulset", "opensearch-cluster-single")
    assert after["metadata"]["uid"] != before["metadata"]["uid"] and after["spec"]["podManagementPolicy"] == "Parallel"
    pod = admin_json("-n", "opensearch", "get", "pod", "opensearch-cluster-single-0")
    assert pod["metadata"]["uid"] == pod_before["metadata"]["uid"], "The repair replaced the OpenSearch Pod"
    owners = [ref["uid"] for ref in pod["metadata"].get("ownerReferences", [])]
    assert owners == [after["metadata"]["uid"]], owners
    data = admin_json("-n", "opensearch", "get", "persistentvolumeclaim", claim["name"])
    assert (data["metadata"]["uid"], data["spec"]["volumeName"]) == (claim["uid"], claim["volume"]), data

    # 10. De-adoption of every namespace, in the reverse of the adoption order.
    block("capture-identities", STEP="before-de-adoption")
    for ns in reversed(ORDER):
        assert f"sh.helm.release.v1.{release_phases.COMPANION}.v1" in block("deadopt-records", ns=ns).stdout
        assert "release: not found" in block("deadopt-delete", ns=ns).stdout
        block("deadopt-plan", ns=ns)
        block("deadopt-apply", ns=ns)
        out = block("deadopt-compare", ns=ns).stdout
        assert "Review the difference" not in out and "Compared" in out, out
    block("capture-identities", STEP="after-de-adoption")
    block("compare-identities", FROM="before-de-adoption", STEP="after-de-adoption")
    assert supplemental() == unadopted, "De-adoption must restore every label and annotation and keep every UID"
    for ns in ORDER:
        block("ownership-plan", ns=ns)
        assert all(item["patch"] for item in json.loads((record_dir / f"ownership-{ns}.json").read_text()))
    assert list(record_dir.glob("ownership-*.*.json")), "The plan block keeps an earlier plan"
    assert all(latest()[key] is None for key in COMPANIONS) and all(latest()[key][1] == "deployed" for key in APPLICATIONS)

    missing = sorted(set(BLOCKS) - set(ran))
    assert not missing, f"Blocks the walk never ran: {missing}"
    print(json.dumps({"ranAsDeployer": True, "blocksRun": len(ran), "distinctBlocks": len(set(ran)),
                      "companionsAdopted": list(ORDER), "adoptionAndNoOpChangedNothing": True,
                      "adoptedRevisions": {key: value[0] for key, value in adopted_revisions.items()},
                      "failedFirstInstallRetried": True, "failedRefusedInLiveModeAndRolledBack": True,
                      "pendingRefusedAndRolledBack": True, "rollbackRefusals": 2, "rollbackUndoneByNextApply": True,
                      "statefulSetRecreatedOnSamePodAndClaim": True,
                      "deAdoptedAll": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--context", choices=(CONTEXT,), required=True)
    args = parser.parse_args()
    if not __debug__:
        parser.error("Enable fixture assertions")
    exercise(args.kubeconfig, args.context)
