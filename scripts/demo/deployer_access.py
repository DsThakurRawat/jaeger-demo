#!/usr/bin/env python3

# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Bind and check the committed deployer roles in ops/rbac/deployer.yaml.

`bindings` prints the role bindings for an administrator to apply. `check` asks
the cluster, through SubjectAccessReviews, whether an identity has exactly the
access this module derives from the chart inventory and the deployment scripts.
"""

import argparse
from collections import namedtuple
import json
from pathlib import Path
import re
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[2]
ROLES = ROOT / "ops/rbac/deployer.yaml"
INVENTORY = ROOT / "ops/rbac/chart-objects.json"
RBAC = "rbac.authorization.k8s.io"
SNAPSHOTS = "snapshot.storage.k8s.io"
RELEASE_NAMESPACES = ("default", "jaeger", "opensearch", "otel-demo")
OTEL_NAMESPACES = RELEASE_NAMESPACES[1:]
DEPLOYER, RECOVERY = "jaeger-demo-deployer", "jaeger-demo-recovery"
# The scheduled identity never runs the recovery or volume expansion scripts.
IDENTITIES = {"scheduled": (DEPLOYER,), "manual": (DEPLOYER, RECOVERY)}
USER = re.compile(r"ocid1\.user\.oc[0-9]+\.\.[a-z0-9]+")
COLLECTION = {"create", "list", "watch"}
# Where a binding references a role that does not share its name. Read from the
# pinned chart renders; a default of the binding's own name covers the rest.
OPENSEARCH_POD = "opensearch-cluster-single-0"
ROLE_REFS = {
    ("", "prometheus-grafana-clusterrolebinding"): "prometheus-grafana-clusterrole",
    ("opensearch", "opensearch-dashboards-dashboards-rolebinding"): "opensearch-dashboards-dashboards",
}

# namespace "" is cluster scope. name "" is the collection, or any name where the
# script chooses it at run time.
Request = namedtuple("Request", "namespace group resource verb name")


def load_roles(path=ROLES):
    roles = {}
    for doc in yaml.safe_load_all(Path(path).read_text()):
        key = doc["metadata"]["name"], doc["metadata"].get("namespace", "")
        if doc["kind"] != ("Role" if key[1] else "ClusterRole") or key in roles:
            raise ValueError(f"Unexpected or repeated role {doc['kind']} {key}")
        roles[key] = doc["rules"]
    return roles


def allows(rule, request):
    names = rule.get("resourceNames")
    return (request.group in rule["apiGroups"] and request.resource in rule["resources"]
            and request.verb in rule["verbs"] and (names is None or request.name in names))


def allowed(identity, request, roles):
    # A ClusterRole bound by a ClusterRoleBinding applies in every namespace.
    return any(allows(rule, request) for (name, namespace), rules in roles.items()
               if name in IDENTITIES[identity] and namespace in ("", request.namespace) for rule in rules)


def chart_requests(records):
    """What Helm and kubectl need for the objects the six releases own.

    The verbs cover a release stored with either apply method: Helm patches an
    existing object in both, and with server-side apply it also creates by patch.
    """
    for record in records:
        namespace = "" if record["scope"] == "cluster" else record["scope"]
        group, resource, name = record["apiGroup"], record["resource"], record["name"]
        verbs = set(record["verbs"])
        if record.get("hookEvents"):
            verbs.add("patch")  # Helm applies a hook by patch when the release uses server-side apply
        if name.startswith("sh.helm.release.v1."):
            verbs.add("list")  # helm status, history and upgrade list the release records
            # Helm numbers every revision. Access that names today's revisions passes
            # until the next upgrade and then fails part-way, so ask for the kind.
            name = ""
        else:
            verbs |= {"list", "watch"}  # helm --wait and kubectl rollout watch the kind
            verbs.add("delete")  # Helm deletes an object when a chart stops rendering it
            if namespace in RELEASE_NAMESPACES:
                verbs.add("create")  # a chart change adds an object
        for verb in sorted(verbs):
            yield Request(namespace, group, resource, verb, "" if verb in COLLECTION else name)
        if resource in ("roles", "clusterroles"):
            # Kubernetes checks escalation on the named role for a patch and on
            # the collection, which has no name, for a create.
            yield Request(namespace, group, resource, "escalate", name)
            if "create" in verbs:
                yield Request(namespace, group, resource, "escalate", "")
        if resource in ("rolebindings", "clusterrolebindings"):
            role = ROLE_REFS.get((namespace, name), name)
            yield Request(namespace, group, resource[:-len("bindings")] + "s", "bind", role)


def script_requests(identity):
    """What the scripts need beyond the chart objects, by the command that needs it."""
    def each(namespace, group, resource, verbs, name=""):
        return [Request(namespace, group, resource, verb, name) for verb in verbs]

    requests = each("kube-system", "", "services", ["list"])  # kubectl cluster-info
    for namespace in OTEL_NAMESPACES:  # examples/otel-demo/deploy-all.sh
        requests += each("", "", "namespaces", ["get"], namespace)
        requests += each(namespace, "cert-manager.io", "certificates", ["list"])
        requests += each(namespace, "", "endpoints", ["get", "list"])
        requests += each(namespace, "", "events", ["list"])
        requests += each(namespace, "", "pods/log", ["get"])
    requests += each("otel-demo", "discovery.k8s.io", "endpointslices", ["list"])  # kubectl describe svc
    for namespace in RELEASE_NAMESPACES:
        # helm --wait follows a workload to the ReplicaSets and Pods it creates.
        requests += each(namespace, "", "pods", ["get", "list", "watch"])
        requests += each(namespace, "apps", "replicasets", ["get", "list", "watch"])
    requests += each("opensearch", "", "persistentvolumeclaims", ["get"])  # deploy.py binding check
    requests += each("opensearch", "", "pods/exec", ["get", "create"], OPENSEARCH_POD)  # verify_trace.py
    requests += each("jaeger", "", "pods/portforward", ["get", "create"])  # verify_trace.py
    if RECOVERY in IDENTITIES[identity]:
        # opensearch-recovery.sh, opensearch-expand-volume.sh and the workflow diagnostics.
        requests += each("", "", "persistentvolumes", ["get"])
        requests += each("", "storage.k8s.io", "storageclasses", ["get"], "oci-bv")
        requests += each("", SNAPSHOTS, "volumesnapshotclasses", ["get"], "jaeger-opensearch-oci-retain")
        requests += each("", SNAPSHOTS, "volumesnapshotclasses", ["create"])
        requests += each("", SNAPSHOTS, "volumesnapshotcontents", ["get", "patch"])
        requests += each("opensearch", SNAPSHOTS, "volumesnapshots", ["get", "list", "create", "patch"])
        requests += each("opensearch", "", "persistentvolumeclaims", ["list", "create", "patch"])
        requests += each("opensearch", "", "pods", ["create", "delete"])
        requests += each("opensearch", "", "pods/exec", ["get", "create"])  # the generated restore Pod
    return requests


def required(identity, inventory=INVENTORY):
    records = json.loads(Path(inventory).read_text())["records"]
    return sorted(set(chart_requests(records)) | set(script_requests(identity)))


def denied(identity):
    """Access neither identity may have. `check` proves the cluster agrees."""
    def each(namespace, group, resource, verbs, name=""):
        return [Request(namespace, group, resource, verb, name) for verb in verbs]

    requests = []
    for namespace in ("kube-system", "cert-manager", "ingress-nginx"):
        for name in ("", "example"):  # the collection and one named object
            requests += each(namespace, "", "secrets", ["get", "list", "create", "update", "delete"], name)
            requests += each(namespace, "", "pods", ["create", "delete"], name)
            requests += each(namespace, "", "pods/exec", ["get", "create"], name)
            requests += each(namespace, "", "pods/portforward", ["get", "create"], name)
            requests += each(namespace, "", "serviceaccounts/token", ["create"], name)
            requests += each(namespace, "apps", "deployments", ["create", "patch", "delete"], name)
            requests += each(namespace, "", "configmaps", ["patch"], name)
            requests += each(namespace, RBAC, "roles", ["create", "patch", "bind", "escalate"], name)
            requests += each(namespace, RBAC, "rolebindings", ["create", "patch"], name)
    # kube-system Services outside the five the monitoring chart owns.
    requests += each("kube-system", "", "services", ["get", "patch", "delete", "create"], "kube-dns")
    for webhook in ("mutatingwebhookconfigurations", "validatingwebhookconfigurations"):
        requests += each("", "admissionregistration.k8s.io", webhook, ["get", "patch", "delete"], "cert-manager-webhook")
        requests += each("", "admissionregistration.k8s.io", webhook, ["create", "delete"], )
    requests += each("", "", "users", ["impersonate"], "example")
    requests += each("", "", "nodes/proxy", ["get", "create"], "example")
    requests += each("", "certificates.k8s.io", "certificatesigningrequests/approval", ["update"], "example")
    requests += each("", "", "secrets", ["list"])  # every namespace at once
    requests += each("", "", "namespaces", ["create"])
    for namespace in RELEASE_NAMESPACES:
        requests += each("", "", "namespaces", ["delete", "patch"], namespace)
        requests += each(namespace, "", "persistentvolumeclaims", ["delete", "deletecollection"])
        requests += each(namespace, "", "persistentvolumeclaims", ["delete"], "example")
        requests += each(namespace, SNAPSHOTS, "volumesnapshots", ["delete", "deletecollection"])
        requests += each(namespace, "cert-manager.io", "certificates", ["create", "patch", "delete"])
    requests += each("", "", "nodes", ["get", "patch"])
    requests += each("", "", "persistentvolumes", ["patch", "delete"])
    requests += each("", "cert-manager.io", "clusterissuers", ["get", "patch"])
    requests += each("", "apiextensions.k8s.io", "customresourcedefinitions", ["create", "patch", "delete"])
    requests += each("", "admissionregistration.k8s.io", "validatingadmissionpolicies", ["create", "delete"])
    requests += each("", "admissionregistration.k8s.io", "validatingadmissionpolicybindings", ["create", "delete"])
    requests += each("", RBAC, "clusterroles", ["patch", "delete", "bind", "escalate"], "cluster-admin")
    requests += each("", RBAC, "clusterrolebindings", ["patch", "delete"], "cluster-admin")
    for role in (DEPLOYER, RECOVERY):  # no rule lets an identity edit its own cluster roles directly
        requests += each("", RBAC, "clusterroles", ["patch", "update", "delete", "bind", "escalate"], role)
        requests += each("", RBAC, "clusterrolebindings", ["patch", "update", "delete"], role)
    if RECOVERY not in IDENTITIES[identity]:
        # Everything the recovery role adds must be out of the scheduled identity's reach.
        requests += set(script_requests("manual")) - set(script_requests(identity))
    return sorted(set(requests))


def unexplained(identity, roles, requests):
    """Every verb a rule grants must answer a request, so the role cannot outgrow its reasons."""
    known = {(r.namespace, r.group, r.resource, r.verb, r.name) for r in requests}
    kinds = {key[:4] for key in known}
    extra = []
    for (name, namespace), rules in sorted(roles.items()):
        if name not in IDENTITIES[identity]:
            continue
        for rule in rules:
            for group in rule["apiGroups"]:
                for resource in rule["resources"]:
                    for verb in rule["verbs"]:
                        for target in rule.get("resourceNames", [None]):
                            # A cluster rule needs a cluster-scoped reason: it applies in every namespace.
                            if target is None:
                                # In the release namespaces a rule covers a kind, because charts
                                # rename and add objects. Anywhere else an unnamed rule needs a
                                # request that itself has no name.
                                found = ((namespace, group, resource, verb, "") in known
                                         or namespace in RELEASE_NAMESPACES and (namespace, group, resource, verb) in kinds)
                            else:
                                found = (namespace, group, resource, verb, target) in known
                            if not found:
                                extra.append((name, namespace, group, resource, verb, target))
    return extra


def bindings(manual, scheduled=None):
    """Bindings for the manual identity, and for the scheduled one once it is named."""
    users = {"scheduled": scheduled, "manual": manual}
    for identity, user in users.items():
        if not (user is None and identity == "scheduled") and not USER.fullmatch(user or ""):
            raise ValueError("Each identity must be an OCI user OCID (ocid1.user.oc1..)")
    if scheduled == manual:
        raise ValueError("The scheduled and manual identities must be different OCI users")
    items = []
    for (name, namespace), _ in sorted(load_roles().items()):
        subjects = [{"apiGroup": RBAC, "kind": "User", "name": users[identity]}
                    for identity, names in IDENTITIES.items() if name in names and users[identity]]
        metadata = {"name": name, **({"namespace": namespace} if namespace else {})}
        items.append({"apiVersion": RBAC + "/v1", "kind": "RoleBinding" if namespace else "ClusterRoleBinding",
                      "metadata": metadata, "subjects": subjects,
                      "roleRef": {"apiGroup": RBAC, "kind": "Role" if namespace else "ClusterRole", "name": name}})
    return {"apiVersion": "v1", "kind": "List", "items": items}


def review(user, request, groups=()):
    """A review of `user`, or of whoever sends it when `user` is None."""
    resource, _, subresource = request.resource.partition("/")
    attributes = {"namespace": request.namespace, "group": request.group, "resource": resource,
                  "subresource": subresource, "verb": request.verb, "name": request.name}
    spec = {"resourceAttributes": {k: v for k, v in attributes.items() if v}}
    if user is None:
        return {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview", "spec": spec}
    spec.update(user=user, **({"groups": list(groups)} if groups else {}))
    return {"apiVersion": "authorization.k8s.io/v1", "kind": "SubjectAccessReview", "spec": spec}


def documents(text):
    """kubectl create prints one JSON document per created object, or a List."""
    decoder, position, items = json.JSONDecoder(), 0, []
    while position < len(text):
        if text[position].isspace():
            position += 1
            continue
        document, position = decoder.raw_decode(text, position)
        items += document["items"] if document.get("kind") == "List" else [document]
    return items


def create(reviews, run=subprocess.run):
    """Send the reviews through kubectl and return the cluster's answers in order."""
    # One request per review. The timeout bounds each one, so a hung API cannot hold the job.
    result = run(["kubectl", "create", "--request-timeout=30s", "-f", "-", "-o", "json"], text=True,
                 capture_output=True, input=json.dumps({"apiVersion": "v1", "kind": "List", "items": reviews}))
    if result.returncode != 0:
        # Say why: an expired credential, an unreachable API and a throttled client look alike otherwise.
        raise ValueError("kubectl could not complete the access reviews: "
                         + (result.stderr.strip() or "exit status %d" % result.returncode))
    return documents(result.stdout)


def check(identity, user, run=None, groups=(), only_required=False):
    """Return the requests whose live answer differs from the committed role."""
    expected = [(request, True) for request in required(identity)]
    if not only_required:
        expected += [(request, False) for request in denied(identity)]
    answers = (run or create)([review(user, request, groups) for request, _ in expected])
    if len(answers) != len(expected):
        raise ValueError("The cluster did not answer every access review")
    differences = []
    for (request, want), answer in zip(expected, answers):
        status = answer.get("status", {})
        # An unanswered review must not pass as a denial.
        if not isinstance(status.get("allowed"), bool) or status.get("evaluationError"):
            raise ValueError("The cluster could not evaluate " + json.dumps(request._asdict(), sort_keys=True))
        echoed = answer.get("spec", {}).get("resourceAttributes", {})
        if any(echoed.get(key) != value for key, value in review(None, request)["spec"]["resourceAttributes"].items()):
            raise ValueError("An access review answer does not match its request")
        if status["allowed"] != want:
            differences.append((request, want))
    return differences


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    bind = commands.add_parser("bindings", help="Print the bindings for kubectl apply -f -")
    bind.add_argument("--manual-user", required=True, help="OCID of the demo-apply OCI user")
    bind.add_argument("--scheduled-user", help="OCID of the demo-scheduled OCI user; without it the deployer bindings name the manual user only")
    live = commands.add_parser("check", help="Compare an identity's live access with the committed role")
    live.add_argument("--identity", choices=sorted(IDENTITIES), required=True)
    who = live.add_mutually_exclusive_group(required=True)
    who.add_argument("--user", help="OCID of that identity's OCI user, reviewed by an administrator")
    who.add_argument("--self", action="store_true", dest="as_self",
                     help="Review the current credential's own required access, without the denied list")
    live.add_argument("--group", action="append", default=[], help="A group the user belongs to; repeat as needed")
    args = parser.parse_args(argv)
    if args.command == "bindings":
        print(json.dumps(bindings(args.manual_user, args.scheduled_user), indent=2))
        return 0
    if not args.as_self and not USER.fullmatch(args.user):
        raise ValueError("The identity must be an OCI user OCID (ocid1.user.oc1..)")
    differences = check(args.identity, None if args.as_self else args.user, groups=args.group,
                        only_required=args.as_self)
    for request, want in differences:
        print(("missing: " if want else "must not be allowed: ") + json.dumps(request._asdict(), sort_keys=True))
    print(f"{args.identity}: {len(differences)} difference(s) from ops/rbac/deployer.yaml")
    return int(bool(differences))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
