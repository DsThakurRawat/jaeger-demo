#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Prove the committed deployer role against a real Kubernetes API server.

On a disposable cluster the administrator applies ops/rbac/deployer.yaml, binds
two fixture users with deployer_access.py, and requires `check --user`, with the
groups each user really has, to find no difference for either identity. Under an
impersonating kubeconfig for each user, `kubectl auth whoami` and `check --self`
must agree. The check must then report a removed rule as missing and an extra
grant as not allowed.

The administrator installs the monitoring release as the base of the change
deployed it (the pull request's base commit, otherwise this checkout), by
client-side apply. The scheduled identity, holding only the role, then runs this
checkout's examples/oci/deploy-all.sh as a scheduled monitoring upgrade, which
runs `helm upgrade --wait` with its hooks. The administrator moves the release
back to the base by server-side apply, and the scheduled identity upgrades it
again, so both ways Helm can have stored the production release are covered. The
API audit log must show no request refused to that identity during either
upgrade. The same identity then runs the read-only commands of the Helmfile
path on that release: `helm list -n` for each release namespace, `helm history
--max 1` and `helm diff upgrade` with the Helmfile apply flags, again with no
request refused in the audit log. A probe release shows that an upgrade fails without a rule it needs and
succeeds once the rule is back. Last, Helm must stop that identity before it
changes anything in kube-system. No production credential is used.
"""

import argparse
import base64
import copy
import gzip
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import chart_archives
import deployer_access
import release_phases

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / "scripts/demo"
OCI = "examples/oci"
CONTEXT = "kind-jaeger-demo-deployer-fixture"
NODE = CONTEXT.removeprefix("kind-") + "-control-plane"
# Where kind.yaml beside this file makes the API server write its audit log.
AUDIT_LOG = "/var/log/kubernetes/audit.log"
MANUAL = "ocid1.user.oc1..aaaafixturemanual"
SCHEDULED = "ocid1.user.oc1..aaaafixturescheduled"
PROBE = "deployer-access-probe"
# Every command must finish inside this, which is inside the step limit in ci.yml.
DEADLINE_MINUTES = 24
# Upper bounds of the long phases. The deadline cuts any of them short.
INSTALL_SECONDS, UPGRADE_SECONDS, COMMAND_SECONDS = 600, 480, 120
# The monitoring inputs a checkout deploys, read by seed_inputs().
SEED_FILES = ("deploy-all.sh", "monitoring-values.yaml", "prometheus-svc.yaml")
# Variables that would change what Helm, kubectl or deploy-all.sh do if the caller's environment set them.
OVERRIDES = ("KUBECONFIG", "KUBECTL_", "HELM_KUBE", "HELM_NAMESPACE", "HELM_DRIVER", "HELM_MAX_HISTORY",
             "HELM_DEBUG", "JAEGER_", "EXPECTED_CONFIG_SHA", "PROMETHEUS_STACK_CHART_VERSION",
             "RUN_PUBLIC_SMOKE_TESTS", "GITHUB_")
# A forbidden answer names the user, the verb, the resource and the namespace. Helm may quote it.
QUOTE = r'\\?"'
REFUSAL = re.compile(rf'User {QUOTE}([^"\\]+){QUOTE} cannot ([a-z]+) resource {QUOTE}([^"\\]+){QUOTE} '
                     rf'in API group {QUOTE}([^"\\]*){QUOTE} in the namespace {QUOTE}([^"\\]+){QUOTE}')
PROBE_TEMPLATE = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: {name}
data:
  purpose: deployer access rehearsal
{{{{- if .Values.ingress }}}}
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: {name}
spec:
  rules:
    - host: {name}.invalid
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: {name}
                port:
                  number: 80
{{{{- end }}}}
{{{{- if .Values.kubeSystem }}}}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: {name}
  namespace: kube-system
data:
  purpose: deployer access rehearsal
{{{{- end }}}}
"""


def kubeconfig(source, context, path, user=None):
    """Write `source` selecting `context`, impersonating `user` when given, readable by its owner only."""
    config = copy.deepcopy(source)
    if context not in [entry["name"] for entry in config.get("contexts") or []]:
        raise ValueError("The kubeconfig has no context " + context)
    config["current-context"] = context
    if user:
        for entry in config.get("users") or []:
            entry["user"] = dict(entry.get("user") or {}, **{"as": user})
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        yaml.safe_dump(config, output)
    return Path(path)


def refusals(text, user, namespace):
    """The (verb, resource) pairs that a forbidden answer in `text` refused `user` in `namespace`."""
    return sorted({(verb, resource) for who, verb, resource, _, where in REFUSAL.findall(text)
                   if who == user and where == namespace})


def without_rule(documents, namespace, resources, named):
    """A copy of the role documents without the one deployer rule for `resources` in `namespace`.

    `named` selects the rule that lists resourceNames, or the one that does not.
    Returns the documents and the removed rule.
    """
    result = copy.deepcopy(documents)
    roles = [doc for doc in result if doc["metadata"]["name"] == deployer_access.DEPLOYER
             and doc["metadata"].get("namespace", "") == namespace]
    matches = [rule for role in roles for rule in role["rules"]
               if rule["resources"] == list(resources) and bool(rule.get("resourceNames")) == named]
    if len(roles) != 1 or len(matches) != 1:
        raise ValueError(f"Expected one deployer rule for {resources} in {namespace or 'the cluster'}")
    roles[0]["rules"].remove(matches[0])
    return result, matches[0]


def differences(output):
    """Parse `deployer_access.py check` output into (Request, required) pairs, as check() returns them."""
    found = []
    for line in output.splitlines():
        for prefix, want in (("missing: ", True), ("must not be allowed: ", False)):
            if line.startswith(prefix):
                found.append((deployer_access.Request(**json.loads(line[len(prefix):])), want))
    return found


def monitoring_command(script):
    """The words after `helm "${HELM_PROM_CMD[@]}"` in deploy-all.sh: the release, chart, values and flags."""
    commands = re.findall(r'^ *helm "\$\{HELM_PROM_CMD\[@\]\}" ((?:.*\\\n)*.*)$', script, re.MULTILINE)
    if len(commands) != 1:
        raise ValueError("Expected one monitoring helm command in deploy-all.sh")
    words = shlex.split(commands[0].replace("\\\n", " "))
    if words.count("prometheus") != 1 or words.count("$PROMETHEUS_STACK_CHART") != 1:
        raise ValueError("The monitoring helm command no longer names the prometheus release and chart")
    return words


def diff_command(words, archive):
    """`helm diff upgrade` of the monitoring release with its deploy-all.sh words, as Helmfile apply runs it.

    The flags are the Helmfile path's: no install, reset values, Secret data hidden. helm-diff has no
    --timeout, so that pair is dropped.
    """
    words = list(words)
    if "--timeout" in words:
        at = words.index("--timeout")
        del words[at:at + 2]
    if any(word.startswith("--timeout") for word in words) or words.count("$PROMETHEUS_STACK_CHART") != 1:
        raise ValueError("Unexpected monitoring helm words for helm diff")
    words = [str(archive) if word == "$PROMETHEUS_STACK_CHART" else word for word in words]
    return ["helm", "diff", "upgrade", *words, "--namespace", "default", "--reset-values",
            "--allow-unreleased=false", "--suppress-secrets"]


def seed_inputs(root):
    """What the checkout at `root` deploys for monitoring: chart pin, helm words and input files."""
    directory = Path(root) / OCI
    missing = [name for name in SEED_FILES if not (directory / name).is_file()]
    if missing or not (Path(root) / "scripts/demo/chart_archives.py").is_file():
        raise ValueError("The checkout lacks " + ", ".join(missing or ["scripts/demo/chart_archives.py"]))
    return {"root": Path(root), "directory": directory,
            "version": chart_archives.pin(Path(root), "PROMETHEUS_STACK_CHART_VERSION"),
            "words": monitoring_command((directory / "deploy-all.sh").read_text()),
            "files": {name: (directory / name).read_bytes() for name in SEED_FILES[1:]}}


def same_inputs(first, second):
    return all(first[key] == second[key] for key in ("version", "words", "files"))


def scheduled_env(base, config):
    """The environment deploy.py gives deploy-all.sh for a scheduled oci/monitoring upgrade."""
    env = {key: value for key, value in base.items() if not key.startswith(OVERRIDES)}
    # The deploy workflow sets RUN_PUBLIC_SMOKE_TESTS; those checks reach the public demo, not this fixture.
    env.update(KUBECONFIG=str(config), GITHUB_EVENT_NAME="schedule", GITHUB_REF="refs/heads/main",
               JAEGER_DEMO_STACK="oci", JAEGER_DEMO_DEPLOY_MODE="upgrade", JAEGER_DEMO_DEPLOY_SCOPE="monitoring",
               JAEGER_OTEL_DEMO_DEPLOY_SCOPE="monitoring", RUN_PUBLIC_SMOKE_TESTS="false")
    return env


def release_record(encoded):
    """Decode the `release` value of a Helm release Secret: the API's base64, then Helm's base64 and gzip."""
    data = base64.b64decode(base64.b64decode(encoded))
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return json.loads(data)


def apply_method(record):
    """How Helm applied a release: "ssa" or "csa". Helm reads a record without the field as client-side."""
    return record.get("apply_method") or "csa"


def forbidden(lines, user, start, end):
    """Requests refused to `user` (HTTP 403) between the audit events of two marker requests.

    The markers are reads of objects named `start` and `end`. Returns "verb resource namespace/name" lines.
    """
    found, inside = [], False
    for line in lines:
        if not line.strip():
            continue
        event = json.loads(line)
        name = (event.get("objectRef") or {}).get("name")
        if name == start:
            inside = True
        elif name == end and inside:
            return sorted(set(found))
        elif inside and (event.get("responseStatus") or {}).get("code") == 403:
            who = (event.get("impersonatedUser") or event.get("user") or {}).get("username")
            if who == user:
                ref = event.get("objectRef") or {}
                resource = "/".join(part for part in (ref.get("resource"), ref.get("subresource")) if part)
                found.append(f"{event.get('verb')} {resource or event.get('requestURI')} "
                             f"{ref.get('namespace', '')}/{ref.get('name', '')}")
    raise ValueError(f"The audit log does not hold both markers {start} and {end}")


def settle(attempt, done, seconds=60, pause=2):
    """Repeat `attempt` until `done` accepts it or `seconds` pass: the authorizer applies a role change within seconds."""
    deadline = time.monotonic() + seconds
    while True:
        result = attempt()
        if done(result) or time.monotonic() >= deadline:
            return result
        time.sleep(pause)


def exercise(cfg, context, baseline):
    source = yaml.safe_load(Path(cfg).read_text())
    base = {key: value for key, value in os.environ.items() if not key.startswith(OVERRIDES)}
    deadline = time.monotonic() + DEADLINE_MINUTES * 60

    def run(args, config=None, body=None, cwd=None, env=None, timeout=COMMAND_SECONDS, stream=False):
        limit = min(timeout, deadline - time.monotonic())
        if limit <= 0:
            raise AssertionError(f"The rehearsal passed its {DEADLINE_MINUTES}-minute deadline")
        env = env or dict(base, KUBECONFIG=str(config))
        output = dict(stdout=sys.stderr, stderr=sys.stderr) if stream else dict(capture_output=True)
        return subprocess.run([str(arg) for arg in args], input=body, cwd=cwd, env=env, text=True,
                              timeout=limit, **output)

    def ok(result):
        if result.returncode:
            raise AssertionError(f"{shlex.join(result.args[:4])} failed ({result.returncode}): "
                                 f"{(result.stderr or '')[-2000:]}{(result.stdout or '')[-1000:]}")
        return (result.stdout or "").strip()

    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        admin = kubeconfig(source, context, temp / "admin.yaml")
        try:
            summary = steps(source, context, baseline, temp, admin, base, run, ok)
        except BaseException:
            # What the cluster looked like when it failed, for the job log.
            for namespace in ("default", "kube-system"):
                subprocess.run(["kubectl", "get", "pods,events", "--namespace", namespace, "-o", "wide"],
                               env=dict(base, KUBECONFIG=str(admin)), stdout=sys.stderr, stderr=sys.stderr,
                               timeout=30, check=False)
            raise
    print(json.dumps(summary, indent=2))


def steps(source, context, baseline, temp, admin, base, run, ok):
    def kube(args, body=None):
        return ok(run(["kubectl", *args], admin, body))

    def helm(args, timeout=COMMAND_SECONDS, **kwargs):
        return ok(run(["helm", *args], admin, timeout=timeout, **kwargs))

    def history(release):
        entries = json.loads(helm(["history", release, "--namespace", "default", "-o", "json"]))
        return [(entry["revision"], entry["status"]) for entry in entries]

    def method(release, revision):
        encoded = kube(["--namespace", "default", "get", "secret", f"sh.helm.release.v1.{release}.v{revision}",
                        "-o", "jsonpath={.data.release}"])
        return apply_method(release_record(encoded))

    view = json.loads(kube(["config", "view", "--minify", "-o", "json"]))
    if urlsplit(view["clusters"][0]["cluster"]["server"]).hostname != "127.0.0.1":
        raise ValueError("This rehearsal requires a disposable loopback API server")
    others = [namespace for namespace in deployer_access.RELEASE_NAMESPACES if namespace != "default"]
    namespaces = set(kube(["get", "ns", "-o", "jsonpath={.items[*].metadata.name}"]).split())
    if namespaces & set(others):
        raise ValueError("Use a new fixture without the demo namespaces")
    server = json.loads(kube(["version", "-o", "json"]))["serverVersion"]["gitVersion"]

    # 1. The release namespaces beside default, and the API server's acceptance of the role,
    # including the empty resource name of the escalate rules.
    for namespace in others:
        kube(["create", "namespace", namespace])
    kube(["apply", "--dry-run=server", "-f", deployer_access.ROLES])
    kube(["apply", "-f", deployer_access.ROLES])

    # 2. Both identities, bound by the committed generator, and an impersonating kubeconfig for
    # each. The API server says who each one is, with the groups it adds.
    kube(["apply", "-f", "-"], json.dumps(deployer_access.bindings(MANUAL, SCHEDULED)))
    users = {"scheduled": SCHEDULED, "manual": MANUAL}
    configs = {identity: kubeconfig(source, context, temp / f"{identity}.yaml", user)
               for identity, user in users.items()}
    groups = {}
    for identity, config in configs.items():
        who = json.loads(ok(run(["kubectl", "auth", "whoami", "-o", "json"], config)))["status"]["userInfo"]
        assert who["username"] == users[identity] and who.get("groups"), (identity, who)
        groups[identity] = who["groups"]

    def check(identity, config=admin):
        who = ["--self"] if config != admin else ["--user", users[identity]]
        if config == admin:
            for group in groups[identity]:
                who += ["--group", group]
        return run([sys.executable, SCRIPTS / "deployer_access.py", "check", "--identity", identity, *who], config)

    def expect(identity, wanted, config=admin):
        """Require the check's exit status and its differences, once the authorizer has the change."""
        code = int(bool(wanted))
        result = settle(lambda: check(identity, config),
                        lambda r: r.returncode == code and differences(r.stdout) == wanted)
        line = f"{identity}: {len(wanted)} difference(s) from ops/rbac/deployer.yaml"
        found = differences(result.stdout)
        assert result.returncode == code and found == wanted, result.stdout + result.stderr
        assert line in result.stdout.splitlines(), result.stdout
        return len(found)

    def reduced_role(namespace, resources, named):
        """Apply the role without one rule and return the required requests the check must now miss."""
        documents = list(yaml.safe_load_all(deployer_access.ROLES.read_text()))
        reduced, removed = without_rule(documents, namespace, resources, named)
        path = temp / f"without-{namespace or 'cluster'}-{'-'.join(resources)}.yaml"
        path.write_text(yaml.safe_dump_all(reduced))
        committed, smaller = deployer_access.load_roles(), deployer_access.load_roles(path)
        missing = [(request, True) for request in deployer_access.required("scheduled")
                   if deployer_access.allowed("scheduled", request, committed)
                   and not deployer_access.allowed("scheduled", request, smaller)]
        assert missing, (namespace, resources)
        kube(["apply", "-f", path])
        return removed, missing

    # 3. Live answers equal the offline model, required and denied, for both identities and
    # their groups.
    checked = {identity: expect(identity, []) for identity in users}

    # 4. Each identity's own credential: what it can do, and that Helm uses it too.
    selves = {}
    for identity, config in configs.items():
        selves[identity] = expect(identity, [], config)
        # Helm reads the same kubeconfig. Its refusal names the user, so it impersonates too.
        listed = run(["helm", "list", "--namespace", "kube-system"], config)
        assert listed.returncode and ("list", "secrets") in refusals(listed.stderr, users[identity], "kube-system"), \
            (identity, listed.returncode, listed.stderr[-2000:])

    # 5. The check notices a missing rule and an extra grant.
    removed, missing = reduced_role("kube-system", ["services"], named=True)
    assert all(request.name in removed["resourceNames"] for request, _ in missing), missing
    detected = {"removedRule": expect("scheduled", missing)}
    kube(["apply", "-f", deployer_access.ROLES])
    expect("scheduled", [])
    extra = [{"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
              "metadata": {"name": PROBE, "namespace": "kube-system"},
              "rules": [{"apiGroups": [""], "resources": ["secrets"], "verbs": ["get"]}]},
             {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
              "metadata": {"name": PROBE, "namespace": "kube-system"},
              "subjects": [{"apiGroup": "rbac.authorization.k8s.io", "kind": "User", "name": SCHEDULED}],
              "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": PROBE}}]
    extra = json.dumps({"apiVersion": "v1", "kind": "List", "items": extra})
    granted = [(request, False) for request in deployer_access.denied("scheduled")
               if request[:4] == ("kube-system", "", "secrets", "get")]
    assert granted, "The denied list no longer covers kube-system Secrets"
    kube(["apply", "-f", "-"], extra)
    detected["extraGrant"] = expect("scheduled", granted)
    kube(["delete", "-f", "-"], extra)
    expect("scheduled", [])

    # 6. The administrator puts the monitoring release where production has it before this change:
    # the base commit's chart pin, values, flags and Services, through the base's chart lock.
    # Client-side apply first: a release Helm 3 stored has that method, and Helm then creates
    # each hook object with a create request rather than an apply patch.
    head = seed_inputs(ROOT)
    seed, reason = head, "no base checkout"
    if baseline:
        try:
            candidate = seed_inputs(baseline)
            reason = "the base deploys the same monitoring inputs" if same_inputs(candidate, head) else None
            seed = head if reason else candidate
        except (OSError, ValueError) as error:
            reason = f"the base checkout cannot seed the release ({error})"
    if reason:
        print(f"Seeding the monitoring release from this checkout: {reason}. "
              "No base to head transition is exercised.", file=sys.stderr)
    archive = ok(run([sys.executable, seed["root"] / "scripts/demo/chart_archives.py", "pull",
                      "kube-prometheus-stack", seed["version"], temp], admin))
    words = [archive if word == "$PROMETHEUS_STACK_CHART" else word for word in seed["words"]]

    def seed_release(*how, timeout=UPGRADE_SECONDS):
        kube(["apply", "-f", seed["directory"] / "prometheus-svc.yaml"])
        helm(["upgrade", *how, "--wait", *words], timeout=timeout, cwd=seed["directory"], stream=True)

    seed_release("--install", "--server-side=false", timeout=INSTALL_SECONDS)

    # 7. The scheduled identity runs this checkout's scheduled monitoring upgrade, hooks and
    # Helm's default wait included, on a client-side release and then, after the
    # administrator returns the release to the base by server-side apply, on a server-side one.
    # The audit log must show no request refused to it meanwhile, including any Helm ignored.
    audit = {}

    def audited(name, action):
        start, end = f"{PROBE}-{name}-start", f"{PROBE}-{name}-end"
        marker = ["kubectl", "--namespace", "default", "get", "configmap", "--ignore-not-found"]
        ok(run([*marker, start], configs["scheduled"]))
        action()
        ok(run([*marker, end], configs["scheduled"]))
        # The end marker's event can reach the file just after its response.
        log = settle(lambda: ok(run(["docker", "exec", NODE, "cat", AUDIT_LOG])), lambda text: end in text, 15)
        refused = forbidden(log.splitlines(), SCHEDULED, start, end)
        assert refused == [], f"The API server refused the scheduled identity during {name}: {refused}"
        audit[name] = len(refused)

    def scheduled_upgrade():
        result = run(["bash", ROOT / OCI / "deploy-all.sh", "upgrade"], cwd=ROOT, timeout=UPGRADE_SECONDS,
                     stream=True, env=scheduled_env(base, configs["scheduled"]))
        assert result.returncode == 0, f"deploy-all.sh upgrade failed as the scheduled identity ({result.returncode})"

    audited("client-side-upgrade", scheduled_upgrade)
    seed_release("--server-side=true", "--force-conflicts")
    audited("server-side-upgrade", scheduled_upgrade)
    revisions = history("prometheus")
    methods = {revision: method("prometheus", revision) for revision, _ in revisions}
    assert revisions == [(1, "superseded"), (2, "superseded"), (3, "superseded"), (4, "deployed")], revisions
    assert methods == {1: "csa", 2: "csa", 3: "ssa", 4: "ssa"}, methods
    by = {1: "administrator, seed", 2: "scheduled, head", 3: "administrator, seed", 4: "scheduled, head"}
    upgrades = [{"revision": revision, "by": by[revision], "applyMethod": methods[revision], "status": status}
                for revision, status in revisions]

    # 8. The read-only commands of the Helmfile path, as the scheduled identity on the release it just
    # upgraded: the listing of each release namespace from release_phases.list_args, the status read by
    # name, and helm-diff, whose Helm 4 render (helm template --dry-run=server --is-upgrade) runs the
    # monitoring chart's lookup calls. The audit log must show no request refused meanwhile.
    head_archive = archive if seed is head else ok(run(
        [sys.executable, SCRIPTS / "chart_archives.py", "pull", "kube-prometheus-stack", head["version"], temp], admin))
    reads = {}

    def helmfile_reads():
        for namespace in deployer_access.RELEASE_NAMESPACES:
            listing = json.loads(ok(run(["helm", *release_phases.list_args(namespace)], configs["scheduled"])))
            reads[namespace] = sorted(entry["name"] for entry in listing)
        latest = json.loads(ok(run(["helm", "history", "prometheus", "-n", "default", "--max", "1", "-o", "json"],
                                   configs["scheduled"])))
        assert [entry["status"] for entry in latest] == ["deployed"], latest
        ok(run(diff_command(head["words"], head_archive), configs["scheduled"], cwd=ROOT / OCI,
               timeout=UPGRADE_SECONDS))

    audited("helmfile-reads", helmfile_reads)
    assert reads["default"] == ["prometheus"], reads

    # 9. A probe release. Without the rule it needs the scheduled identity's upgrade fails, as
    # that user, before it changes anything; with the committed role the same upgrade succeeds.
    chart = temp / PROBE
    (chart / "templates").mkdir(parents=True)
    (chart / "Chart.yaml").write_text(f"apiVersion: v2\nname: {PROBE}\nversion: 0.1.0\n")
    (chart / "values.yaml").write_text("ingress: false\nkubeSystem: false\n")
    (chart / "templates/objects.yaml").write_text(PROBE_TEMPLATE.format(name=PROBE))
    helm(["install", PROBE, chart, "--namespace", "default", "--wait"])
    add_ingress = ["helm", "upgrade", PROBE, chart, "--namespace", "default", "--set", "ingress=true"]
    _, lost = reduced_role("default", ["ingresses"], named=False)
    expect("scheduled", lost)
    result = run(add_ingress, configs["scheduled"])
    found = refusals(result.stderr, SCHEDULED, "default")
    assert result.returncode and found and {resource for _, resource in found} == {"ingresses"}, \
        (result.returncode, result.stderr[-2000:])
    assert history(PROBE) == [(1, "deployed")], history(PROBE)
    control = {"withoutRule": {"missing": len(lost), "refused": [" ".join(pair) for pair in found]}}
    kube(["apply", "-f", deployer_access.ROLES])
    expect("scheduled", [])
    audited("probe-upgrade", lambda: ok(run(add_ingress, configs["scheduled"])))
    assert history(PROBE) == [(1, "superseded"), (2, "deployed")], history(PROBE)
    control["withRule"] = {"revision": history(PROBE)[-1][0], "status": history(PROBE)[-1][1]}

    # 10. Helm stops the scheduled identity before it changes anything in kube-system: its check of
    # whether each new object already exists is refused. Server dry runs, so a gap that let the
    # check through would still change nothing.
    before = {release: history(release) for release in ("prometheus", PROBE)}
    refused = {}
    for case, args in (("install into kube-system", ["install", PROBE, chart, "--namespace", "kube-system"]),
                       ("upgrade adding a kube-system ConfigMap",
                        ["upgrade", PROBE, chart, "--namespace", "default", "--set", "ingress=true",
                         "--set", "kubeSystem=true"])):
        result = run(["helm", *args, "--dry-run=server"], configs["scheduled"])
        found = refusals(result.stderr, SCHEDULED, "kube-system")
        assert result.returncode and found and {resource for _, resource in found} == {"configmaps"}, \
            (case, result.returncode, result.stderr[-2000:])
        refused[case] = [" ".join(pair) for pair in found]
    assert {release: history(release) for release in before} == before, "A refused dry run changed a release"
    assert kube(["-n", "kube-system", "get", "configmap", PROBE, "--ignore-not-found", "-o", "name"]) == ""

    # The evidence, for the job log, from what was measured.
    return {
        "kubernetes": server, "roleAcceptedByServerDryRun": True, "groups": groups,
        "checkDifferences": checked, "selfCheckDifferences": selves, "detected": detected,
        "monitoring": {"release": "default/prometheus", "scheduledCommand": "examples/oci/deploy-all.sh upgrade",
                       "seed": {"from": "head" if reason else "base", "reason": reason,
                                "chart": Path(archive).name},
                       "headChart": f"kube-prometheus-stack-{head['version']}.tgz", "revisions": upgrades},
        "helmfileReads": {"listed": reads, "diff": "prometheus"}, "auditedRefusals": audit,
        "upgradeWithoutRule": control, "refusedInKubeSystem": refused, "productionAccess": False,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True, choices=(CONTEXT,))
    parser.add_argument("--base", type=Path, help="Checkout of the pull request's base commit, to seed the release")
    args = parser.parse_args()
    if not __debug__:
        parser.error("Rehearsal assertions must be enabled")
    exercise(args.kubeconfig.resolve(), args.context, args.base.resolve() if args.base else None)
