#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Prove the committed deployer role against a real Kubernetes API server.

On a disposable cluster the administrator applies ops/rbac/deployer.yaml, binds
two fixture users with deployer_access.py, and requires `check --user` to find no
difference for either identity. Under an impersonating kubeconfig for each user,
`kubectl auth whoami` and `check --self` must agree. The check must then report a
removed rule as missing and an extra grant as not allowed.

The administrator installs the monitoring release from the lock-verified archive
with the production values and flags. The scheduled identity, holding only the
role, then runs the scheduled monitoring upgrade through
examples/oci/deploy-all.sh, which runs `helm upgrade --wait` with its hooks. Last,
the API server must refuse that identity a release in kube-system and an upgrade
that adds an object there. No production credential is used.
"""

import argparse
import copy
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

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / "scripts/demo"
OCI = ROOT / "examples/oci"
CONTEXT = "kind-jaeger-demo-deployer-fixture"
MANUAL = "ocid1.user.oc1..aaaafixturemanual"
SCHEDULED = "ocid1.user.oc1..aaaafixturescheduled"
PROBE = "deployer-access-probe"
# Variables that would change what deploy-all.sh does if the caller's environment set them.
OVERRIDES = ("KUBECONFIG", "HELM_KUBE", "HELM_NAMESPACE", "JAEGER_", "EXPECTED_CONFIG_SHA",
             "PROMETHEUS_STACK_CHART_VERSION", "RUN_PUBLIC_SMOKE_TESTS", "GITHUB_")
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
    """Parse `deployer_access.py check` output into (required, Request) pairs, as check() returns them."""
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


def scheduled_env(base, config):
    """The environment deploy.py gives deploy-all.sh for a scheduled oci/monitoring upgrade."""
    env = {key: value for key, value in base.items() if not key.startswith(OVERRIDES)}
    # The deploy workflow sets RUN_PUBLIC_SMOKE_TESTS; those checks reach the public demo, not this fixture.
    env.update(KUBECONFIG=str(config), GITHUB_EVENT_NAME="schedule", GITHUB_REF="refs/heads/main",
               JAEGER_DEMO_STACK="oci", JAEGER_DEMO_DEPLOY_MODE="upgrade", JAEGER_DEMO_DEPLOY_SCOPE="monitoring",
               JAEGER_OTEL_DEMO_DEPLOY_SCOPE="monitoring", RUN_PUBLIC_SMOKE_TESTS="false")
    return env


def settle(attempt, done, tries=30, pause=2):
    """Repeat `attempt` until `done` accepts it: the authorizer applies a role change within seconds."""
    for _ in range(tries):
        result = attempt()
        if done(result):
            break
        time.sleep(pause)
    return result


def exercise(cfg, context):
    source = yaml.safe_load(Path(cfg).read_text())
    base = {key: value for key, value in os.environ.items() if not key.startswith(OVERRIDES)}

    def run(args, config=None, body=None, cwd=None, env=None, timeout=300, stream=False):
        env = env or dict(base, KUBECONFIG=str(config))
        output = dict(stdout=sys.stderr, stderr=sys.stderr) if stream else dict(capture_output=True)
        return subprocess.run([str(arg) for arg in args], input=body, cwd=cwd, env=env, text=True,
                              timeout=timeout, **output)

    def ok(result):
        if result.returncode:
            raise AssertionError(f"{shlex.join(result.args[:4])} failed ({result.returncode}): "
                                 f"{(result.stderr or '')[-2000:]}{(result.stdout or '')[-1000:]}")
        return (result.stdout or "").strip()

    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        admin = kubeconfig(source, context, temp / "admin.yaml")

        def kube(args, body=None):
            return ok(run(["kubectl", *args], admin, body))

        def helm(args):
            return ok(run(["helm", *args], admin))

        def helm_history(release):
            entries = json.loads(helm(["history", release, "--namespace", "default", "-o", "json"]))
            return [(entry["revision"], entry["status"]) for entry in entries]

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

        # 2. Both identities, bound by the committed generator.
        kube(["apply", "-f", "-"], json.dumps(deployer_access.bindings(MANUAL, SCHEDULED)))
        users = {"scheduled": SCHEDULED, "manual": MANUAL}

        def check(identity, config=admin):
            who = ["--self"] if config != admin else ["--user", users[identity]]
            return run([sys.executable, SCRIPTS / "deployer_access.py", "check", "--identity", identity, *who],
                       config)

        def expect(identity, wanted, config=admin):
            """Require the check's exit status and its differences, once the authorizer has the change."""
            code = int(bool(wanted))
            result = settle(lambda: check(identity, config),
                            lambda r: r.returncode == code and differences(r.stdout) == wanted)
            summary = f"{identity}: {len(wanted)} difference(s) from ops/rbac/deployer.yaml"
            assert result.returncode == code and differences(result.stdout) == wanted, result.stdout + result.stderr
            assert summary in result.stdout.splitlines(), result.stdout

        # 3. Live answers equal the offline model, required and denied, for both identities.
        for identity in ("scheduled", "manual"):
            expect(identity, [])

        # 4. Each identity's own credential: who it is, and what it can do.
        configs = {identity: kubeconfig(source, context, temp / f"{identity}.yaml", user)
                   for identity, user in users.items()}
        for identity, config in configs.items():
            who = ok(run(["kubectl", "auth", "whoami", "-o", "jsonpath={.status.userInfo.username}"], config))
            assert who == users[identity], (identity, who)
            expect(identity, [], config)
            # Helm reads the same kubeconfig. Its refusal names the user, so it impersonates too.
            listed = run(["helm", "list", "--namespace", "kube-system"], config)
            assert listed.returncode and ("list", "secrets") in refusals(listed.stderr, users[identity], "kube-system"), \
                (identity, listed.returncode, listed.stderr[-2000:])

        # 5. The check notices a missing rule and an extra grant.
        committed = deployer_access.load_roles()
        documents = list(yaml.safe_load_all(deployer_access.ROLES.read_text()))
        reduced, removed = without_rule(documents, "kube-system", ["services"], named=True)
        reduced_path = temp / "reduced.yaml"
        reduced_path.write_text(yaml.safe_dump_all(reduced))
        smaller = deployer_access.load_roles(reduced_path)
        missing = [(request, True) for request in deployer_access.required("scheduled")
                   if deployer_access.allowed("scheduled", request, committed)
                   and not deployer_access.allowed("scheduled", request, smaller)]
        assert missing and all(request.name in removed["resourceNames"] for request, _ in missing), missing
        kube(["apply", "-f", reduced_path])
        expect("scheduled", missing)
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
        expect("scheduled", granted)
        kube(["delete", "-f", "-"], extra)
        expect("scheduled", [])

        # 6. The administrator installs the monitoring release as a manual first install would:
        # the verified archive and the flags and values of deploy-all.sh, run from its directory.
        version = chart_archives.pin(ROOT, "PROMETHEUS_STACK_CHART_VERSION")
        archive = ok(run([sys.executable, SCRIPTS / "chart_archives.py", "pull", "kube-prometheus-stack", version,
                          temp], admin, timeout=600))
        flags = ["upgrade", "--install", "--wait", *monitoring_command((OCI / "deploy-all.sh").read_text())]
        flags[flags.index("$PROMETHEUS_STACK_CHART")] = archive
        # The Services deploy-all.sh applies beside the release exist in production too.
        kube(["apply", "-f", OCI / "prometheus-svc.yaml"])
        ok(run(["helm", *flags], admin, cwd=OCI, timeout=1500, stream=True))
        assert helm_history("prometheus") == [(1, "deployed")], helm_history("prometheus")

        # 7. The scheduled identity runs the scheduled monitoring upgrade, hooks and Helm's default wait included.
        upgrade = run(["bash", OCI / "deploy-all.sh", "upgrade"], cwd=ROOT, timeout=1500, stream=True,
                      env=scheduled_env(base, configs["scheduled"]))
        assert upgrade.returncode == 0, f"deploy-all.sh upgrade failed as the scheduled identity ({upgrade.returncode})"
        assert helm_history("prometheus") == [(1, "superseded"), (2, "deployed")], helm_history("prometheus")

        # 8. Helm cannot take that identity into kube-system, by a new release or by an upgrade.
        chart = temp / PROBE
        (chart / "templates").mkdir(parents=True)
        (chart / "Chart.yaml").write_text(f"apiVersion: v2\nname: {PROBE}\nversion: 0.1.0\n")
        (chart / "values.yaml").write_text("kubeSystem: false\n")
        (chart / "templates/configmaps.yaml").write_text(PROBE_TEMPLATE.format(name=PROBE))
        helm(["install", PROBE, chart, "--namespace", "default", "--wait"])
        before = {release: helm_history(release) for release in ("prometheus", PROBE)}
        refused = {}
        for case, args in (("install into kube-system", ["install", PROBE, chart, "--namespace", "kube-system"]),
                           ("upgrade adding a kube-system ConfigMap",
                            ["upgrade", PROBE, chart, "--namespace", "default", "--set", "kubeSystem=true"])):
            result = run(["helm", *args, "--dry-run=server"], configs["scheduled"])
            found = refusals(result.stderr, SCHEDULED, "kube-system")
            assert result.returncode and found and {resource for _, resource in found} == {"configmaps"}, \
                (case, result.returncode, result.stderr[-2000:])
            refused[case] = [" ".join(pair) for pair in found]
        assert {release: helm_history(release) for release in before} == before, "A refused dry run changed a release"
        assert kube(["-n", "kube-system", "get", "configmap", PROBE, "--ignore-not-found", "-o", "name"]) == ""

    # 9. The evidence, for the job log.
    print(json.dumps({
        "kubernetes": server, "chart": f"kube-prometheus-stack-{version}", "roleAcceptedByServerDryRun": True,
        "checkDifferences": {"scheduled": 0, "manual": 0}, "selfCheckDifferences": {"scheduled": 0, "manual": 0},
        "detected": {"removedRule": len(missing), "extraGrant": len(granted)},
        "scheduledUpgrade": {"command": "examples/oci/deploy-all.sh upgrade", "scope": "monitoring",
                             "event": "schedule", "release": "default/prometheus", "revision": 2,
                             "status": "deployed"},
        "refused": refused, "productionAccess": False,
    }, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kubeconfig", required=True, type=Path)
    parser.add_argument("--context", required=True, choices=(CONTEXT,))
    args = parser.parse_args()
    if not __debug__:
        parser.error("Rehearsal assertions must be enabled")
    exercise(args.kubeconfig.resolve(), args.context)
