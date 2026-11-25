# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""The committed deployer roles must grant what the deployment needs and nothing else."""

import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import deployer_access as d


ROLES = d.load_roles()
R = d.Request
SCHEDULED_USER = "ocid1.user.oc1..aaaaexamplescheduled"
MANUAL_USER = "ocid1.user.oc1..aaaaexamplemanual"
# Kinds that have no namespace. A ClusterRole rule applies in every namespace, so
# it may only name these.
CLUSTER_KINDS = {
    "namespaces", "clusterroles", "clusterrolebindings", "mutatingwebhookconfigurations",
    "validatingwebhookconfigurations", "persistentvolumes", "storageclasses", "customresourcedefinitions",
}


def request_of(spec):
    """The Request an access review asks about."""
    a = spec["resourceAttributes"]
    resource = a["resource"] + ("/" + a["subresource"] if "subresource" in a else "")
    return R(a.get("namespace", ""), a.get("group", ""), resource, a["verb"], a.get("name", ""))


def missing(identity, roles=ROLES):
    return [r for r in d.required(identity) if not d.allowed(identity, r, roles)]


def wrongly_allowed(identity, roles=ROLES):
    return [r for r in d.denied(identity) if d.allowed(identity, r, roles)]


class DeployerAccessTests(unittest.TestCase):
    def test_roles_cover_every_chart_object_and_script_command(self):
        for identity in d.IDENTITIES:
            with self.subTest(identity=identity):
                self.assertEqual(missing(identity), [])
        records = json.loads(d.INVENTORY.read_text())["records"]
        # Guard against a vacuous pass: the inventory holds the six application releases and the
        # four demo-resources companions of the Helmfile path.
        self.assertEqual({(r["scope"], r["release"]) for r in records if r["component"] == "history"},
                         {("default", "jaeger"), ("default", "prometheus"), ("jaeger", "jaeger"),
                          ("opensearch", "opensearch"), ("opensearch", "opensearch-dashboards"),
                          ("otel-demo", "otel-demo")} | {(namespace, "demo-resources") for namespace in d.RELEASE_NAMESPACES})
        self.assertGreater(len(d.required("scheduled")), 800)

    def test_every_granted_verb_answers_a_request(self):
        for identity in d.IDENTITIES:
            with self.subTest(identity=identity):
                self.assertEqual(d.unexplained(identity, ROLES, d.required(identity)), [])

    def test_access_outside_the_demo_is_denied(self):
        for identity in d.IDENTITIES:
            with self.subTest(identity=identity):
                self.assertEqual(wrongly_allowed(identity), [])

    def test_a_dropped_or_added_rule_is_detected(self):
        dropped = copy.deepcopy(ROLES)
        dropped[d.DEPLOYER, ""] = [rule for rule in dropped[d.DEPLOYER, ""] if "escalate" not in rule["verbs"]]
        self.assertIn(R("", d.RBAC, "clusterroles", "escalate", ""), missing("scheduled", dropped))
        self.assertIn(R("", d.RBAC, "clusterroles", "escalate", "prometheus-kube-prometheus-operator"),
                      missing("scheduled", dropped))

        for extra, request in [
            ({"apiGroups": [""], "resources": ["secrets"], "verbs": ["list"]}, R("", "", "secrets", "list", "")),
            ({"apiGroups": [d.RBAC], "resources": ["clusterroles"], "resourceNames": ["cluster-admin"],
              "verbs": ["bind"]}, R("", d.RBAC, "clusterroles", "bind", "cluster-admin")),
        ]:
            with self.subTest(extra=extra):
                added = copy.deepcopy(ROLES)
                added[d.DEPLOYER, ""].append(extra)
                self.assertIn(request, wrongly_allowed("scheduled", added))
                self.assertTrue(d.unexplained("scheduled", added, d.required("scheduled")))

        # Outside the release namespaces a named reason does not excuse an unnamed rule.
        widened = copy.deepcopy(ROLES)
        for rule in widened[d.DEPLOYER, "kube-system"]:
            rule.pop("resourceNames", None)
        self.assertIn(R("kube-system", "", "services", "patch", "kube-dns"), wrongly_allowed("scheduled", widened))
        self.assertEqual({item[4] for item in d.unexplained("scheduled", widened, d.required("scheduled"))},
                         {"get", "patch", "delete"})
        widened = copy.deepcopy(ROLES)
        for rule in widened[d.DEPLOYER, ""]:
            if "clusterrolebindings" in rule["resources"] and "patch" in rule["verbs"]:
                del rule["resourceNames"]
        self.assertIn(R("", d.RBAC, "clusterrolebindings", "patch", "cluster-admin"), wrongly_allowed("scheduled", widened))
        self.assertTrue(d.unexplained("scheduled", widened, d.required("scheduled")))

        # Release records limited to the revisions that exist today are not enough: the
        # next upgrade writes a new one. This is the grant that failed in #87.
        records = sorted(r["name"] for r in json.loads(d.INVENTORY.read_text())["records"]
                         if r["scope"] == "default" and r["name"].startswith("sh.helm.release.v1."))
        self.assertTrue(records)
        named = copy.deepcopy(ROLES)
        for rule in named[d.DEPLOYER, "default"]:
            if rule["resources"] == ["secrets"] and rule["verbs"] == ["update"]:
                rule["resourceNames"] = records
        self.assertEqual([R("default", "", "secrets", "update", "")], missing("scheduled", named))
        for request in d.required("scheduled"):
            self.assertFalse(request.name.startswith("sh.helm.release.v1."), request)

        # A namespaced verb with no reason is found even when nothing lists it as denied.
        added = copy.deepcopy(ROLES)
        added[d.DEPLOYER, "otel-demo"].append({"apiGroups": [""], "resources": ["secrets"], "verbs": ["patch"]})
        self.assertEqual(d.unexplained("scheduled", added, d.required("scheduled")),
                         [(d.DEPLOYER, "otel-demo", "", "secrets", "patch", None)])

    def test_helmfile_commands_are_required_of_both_identities_and_granted(self):
        rows = d.helmfile_requests()
        self.assertEqual([row[0] for row in d.HELMFILE_COMMANDS], list(rows))
        for identity in d.IDENTITIES:
            required = set(d.required(identity))
            for command, requests in rows.items():
                with self.subTest(identity=identity, command=command):
                    self.assertTrue(requests)
                    self.assertEqual({r.namespace for r in requests}, set(d.RELEASE_NAMESPACES))
                    self.assertEqual({(r.group, r.resource, r.name) for r in requests}, {("", "secrets", "")})
                    self.assertLessEqual(set(requests), required)
                    self.assertEqual([r for r in requests if not d.allowed(identity, r, ROLES)], [])
        # The rows hold without the inventory's history records, so dropping those cannot hide a command.
        document = json.loads(d.INVENTORY.read_text())
        document["records"] = [r for r in document["records"] if r["component"] != "history"]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "inventory.json"
            path.write_text(json.dumps(document))
            for identity in d.IDENTITIES:
                self.assertLessEqual({r for requests in rows.values() for r in requests}, set(d.required(identity, path)))

    def test_helmfile_rows_name_the_code_that_runs_them(self):
        """A row whose command has gone, or changed what it asks of Helm, is stale."""
        import release_phases
        for command, code, verbs, why in d.HELMFILE_COMMANDS:
            with self.subTest(command=command):
                self.assertTrue(verbs and why)
                if code.startswith("release_phases."):
                    self.assertTrue(callable(getattr(release_phases, code.split()[0].split(".", 1)[1])))
        listing = release_phases.list_args("otel-demo")
        self.assertEqual(listing, ["list", "-n", "otel-demo", "--max", "0", "-o", "json"])
        install = release_phases.install_args("otel-demo", "chart.tgz", "values.json")
        self.assertEqual(install[:2], ["install", "demo-resources"])
        for flag in ("--server-side=true", "--wait=legacy", "--create-namespace=false"):
            self.assertIn(flag, install)
            self.assertIn(flag, d.HELMFILE_COMMANDS[4][0])
        # The rehearsal that runs the status read and native Helmfile as the bound deployer.
        phases = (d.ROOT / "scripts/demo/rehearsals/release-phases/exercise.py").read_text()
        for text in ['"history", rel, "--max", "1", "-o", "json"', "release_phases.selected_statuses(",
                     "release_phases.run(", '"--install=false"', '"--reset-values"', "--allow-unreleased=false",
                     '"--suppress-secrets"']:
            self.assertIn(text, phases)
        # helm-diff's requests were read from this release; another one must be read again.
        lock = json.loads((d.ROOT / "scripts/demo/helmfile-tools.lock.json").read_text())["tools"]
        self.assertIn("helm-diff " + lock["helm-diff"]["version"].lstrip("v"), d.HELMFILE_COMMANDS[2][1])
        # helm-diff renders with --dry-run=server only under Helm 4; Helm 4 defaults to server-side apply.
        self.assertTrue(lock["helm"]["version"].startswith("v4."), "review the rows for another Helm major")
        # Helm deletes revisions beyond historyMax, which is why an upgrade deletes Secrets.
        self.assertIn("historyMax: 10", (d.ROOT / "helmfile.yaml").read_text())

    def test_check_reports_a_dropped_or_extra_helmfile_grant(self):
        def check_lines(identity, roles):
            def run(reviews):
                return [{"spec": r["spec"], "status": {"allowed": d.allowed(identity, request_of(r["spec"]), roles)}}
                        for r in reviews]
            out = io.StringIO()
            user = SCHEDULED_USER if identity == "scheduled" else MANUAL_USER
            with patch.object(d, "create", run), patch("sys.stdout", out):
                status = d.main(["check", "--identity", identity, "--user", user])
            return status, out.getvalue().splitlines()

        def line(prefix, request):
            return prefix + json.dumps(request._asdict(), sort_keys=True)

        rows = d.helmfile_requests()
        for identity in d.IDENTITIES:
            self.assertEqual((0, [f"{identity}: 0 difference(s) from ops/rbac/deployer.yaml"]), check_lines(identity, ROLES))
            # Without the otel-demo Secret rule, every Helmfile command there is missing.
            dropped = copy.deepcopy(ROLES)
            dropped[d.DEPLOYER, "otel-demo"] = [rule for rule in dropped[d.DEPLOYER, "otel-demo"]
                                                if rule["resources"] != ["secrets"]]
            status, lines = check_lines(identity, dropped)
            self.assertEqual(1, status)
            for command, requests in rows.items():
                for request in requests:
                    if request.namespace == "otel-demo":
                        with self.subTest(identity=identity, command=command, request=request):
                            self.assertIn(line("missing: ", request), lines)
            self.assertFalse([text for text in lines if text.startswith("must not be allowed: ")])
            # A cluster-wide Secret list (a release listing of every namespace) and a collection delete of the
            # release records are extra grants the check names, and the model cannot explain.
            added = copy.deepcopy(ROLES)
            added[d.DEPLOYER, ""].append({"apiGroups": [""], "resources": ["secrets"], "verbs": ["list"]})
            added[d.DEPLOYER, "otel-demo"].append({"apiGroups": [""], "resources": ["secrets"],
                                                   "verbs": ["deletecollection"]})
            status, lines = check_lines(identity, added)
            self.assertEqual(1, status)
            for request in (R("", "", "secrets", "list", ""), R("otel-demo", "", "secrets", "deletecollection", "")):
                with self.subTest(identity=identity, request=request):
                    self.assertIn(line("must not be allowed: ", request), lines)
            self.assertFalse([text for text in lines if text.startswith("missing: ")])
            self.assertEqual({(item[1], item[4]) for item in d.unexplained(identity, added, d.required(identity))},
                             {("", "list"), ("otel-demo", "deletecollection")})

    def test_rules_have_no_wildcards_and_cluster_rules_name_only_cluster_kinds(self):
        for (name, namespace), rules in ROLES.items():
            for rule in rules:
                with self.subTest(role=name, namespace=namespace, rule=rule):
                    self.assertEqual(set(rule) - {"resourceNames"}, {"apiGroups", "resources", "verbs"})
                    for values in rule.values():
                        self.assertNotIn("*", values)
                        self.assertTrue(values)
                    # The empty name stands for the collection, and only an escalation check uses it.
                    if "" in rule.get("resourceNames", []):
                        self.assertEqual((rule["verbs"], rule["resourceNames"], rule["apiGroups"]),
                                         (["escalate"], [""], [d.RBAC]))
                    if not namespace:
                        self.assertLessEqual(set(rule["resources"]), CLUSTER_KINDS)

    def test_roles_exist_only_in_the_demo_namespaces(self):
        self.assertEqual({namespace for name, namespace in ROLES if name == d.DEPLOYER},
                         {"", "default", "jaeger", "opensearch", "otel-demo", "kube-system"})
        self.assertEqual({namespace for name, namespace in ROLES if name == d.RECOVERY}, {"", "opensearch"})
        self.assertEqual({name for name, _ in ROLES}, {d.DEPLOYER, d.RECOVERY})
        # kube-system access is the five named scrape Services and nothing else.
        self.assertEqual({(resource, verb) for rule in ROLES[d.DEPLOYER, "kube-system"]
                          for resource in rule["resources"] for verb in rule["verbs"]},
                         {("services", "get"), ("services", "patch"), ("services", "delete"), ("services", "list"),
                          ("services", "watch")})

    def test_storage_changes_need_the_manual_identity_and_no_role_deletes_storage(self):
        for request in [R("opensearch", "", "persistentvolumeclaims", "patch", "data"),
                        R("", "storage.k8s.io", "storageclasses", "get", "oci-bv"),
                        R("", "", "persistentvolumes", "get", "pv-1")]:
            with self.subTest(request=request):
                self.assertFalse(d.allowed("scheduled", request, ROLES))
                self.assertTrue(d.allowed("manual", request, ROLES))
        # Both identities run a command in the OpenSearch Pod and no other.
        for identity in d.IDENTITIES:
            self.assertTrue(d.allowed(identity, R("opensearch", "", "pods/exec", "create", d.OPENSEARCH_POD), ROLES))
            self.assertFalse(d.allowed(identity, R("opensearch", "", "pods/exec", "create", "opensearch-dashboards-1"), ROLES))
        # Everything the second role adds is on the scheduled identity's denied list, and it is
        # exactly what volume expansion needs.
        manual_only = set(d.script_requests("manual")) - set(d.script_requests("scheduled"))
        self.assertEqual(manual_only, {R("", "", "persistentvolumes", "get", ""),
                                       R("", "storage.k8s.io", "storageclasses", "get", "oci-bv"),
                                       R("opensearch", "", "persistentvolumeclaims", "patch", "")})
        self.assertLessEqual(manual_only, set(d.denied("scheduled")))
        self.assertEqual({(rule["apiGroups"][0], resource, verb) for key in ((d.RECOVERY, ""), (d.RECOVERY, "opensearch"))
                          for rule in ROLES[key] for resource in rule["resources"] for verb in rule["verbs"]},
                         {("", "persistentvolumes", "get"), ("storage.k8s.io", "storageclasses", "get"),
                          ("", "persistentvolumeclaims", "patch")})
        # The role names the storage class it reads.
        self.assertFalse(d.allowed("manual", R("", "storage.k8s.io", "storageclasses", "get", "other"), ROLES))
        # What the removed backup and restore tooling held is denied to both identities (#76).
        retired = [R("opensearch", "", "persistentvolumeclaims", "create", ""),
                   R("opensearch", "", "persistentvolumeclaims", "list", ""),
                   R("opensearch", "", "pods", "create", ""),
                   R("opensearch", "", "pods", "delete", ""),
                   R("opensearch", "", "pods/exec", "create", ""),
                   R("opensearch", d.SNAPSHOTS, "volumesnapshots", "create", ""),
                   R("opensearch", d.SNAPSHOTS, "volumesnapshots", "list", ""),
                   R("", d.SNAPSHOTS, "volumesnapshotcontents", "patch", ""),
                   R("", d.SNAPSHOTS, "volumesnapshotclasses", "create", ""),
                   R("", d.SNAPSHOTS, "volumesnapshotclasses", "get", "jaeger-opensearch-oci-retain")]
        self.assertLessEqual(set(retired), set(d.retired_requests()))
        for identity in d.IDENTITIES:
            self.assertLessEqual(set(d.retired_requests()), set(d.denied(identity)))
            for request in retired + [R("opensearch", "", "pods/exec", "create", "jaeger-os-verify-1-1"),
                                      R("", d.SNAPSHOTS, "volumesnapshotcontents", "patch", "snapcontent-1"),
                                      R("", d.SNAPSHOTS, "volumesnapshotclasses", "get", "other")]:
                with self.subTest(identity=identity, request=request):
                    self.assertFalse(d.allowed(identity, request, ROLES))
            self.assertFalse(set(d.retired_requests()) & set(d.required(identity)))
        for identity in d.IDENTITIES:
            for request in [R("opensearch", "", "persistentvolumeclaims", "delete", "data"),
                            R("", "", "persistentvolumes", "delete", "pv"),
                            R("opensearch", d.SNAPSHOTS, "volumesnapshots", "delete", "snapshot"),
                            R("", "", "namespaces", "delete", "opensearch")]:
                with self.subTest(identity=identity, request=request):
                    self.assertFalse(d.allowed(identity, request, ROLES))

    def test_requests_name_commands_the_scripts_still_run(self):
        """A grant whose command has gone is stale. These are the commands behind script_requests."""
        root = d.ROOT
        for path, commands in {
            "examples/otel-demo/deploy-all.sh": ["kubectl cluster-info", 'kubectl get namespace "$ns"',
                                                 'kubectl get certificate -n "$ns"', "get endpoints", "get events",
                                                 "logs -l opentelemetry.io/name=frontend ",
                                                 "describe svc otel-collector"],
            "scripts/demo/verify_trace.py": ['"exec", "' + d.OPENSEARCH_POD + '"', '"port-forward"'],
            # The trace reset needs nothing beyond what the deployment already holds.
            "scripts/demo/opensearch_reset.py": ['"exec", POD', '"get", "pod", POD', '"rollout", "restart"',
                                                 '"rollout", "status"', '"port-forward"'],
            "scripts/demo/deploy.py": ['"get", kind, name', '"pvc"'],
            "examples/otel-demo/opensearch-expand-volume.sh": ["patch pvc", "get storageclass", "get pv ",
                                                               'STORAGE_CLASS="oci-bv"'],
            # The drift report reads every rendered object, the CRDs included, by name.
            "scripts/demo/live_drift.py": ["from deployer_access import MONITORING_CRDS", "get(item_path(key, routes))"],
        }.items():
            text = (root / path).read_text()
            for command in commands:
                with self.subTest(path=path, command=command):
                    self.assertIn(command, text)

    def test_the_drift_report_reads_the_monitoring_crds_by_name_only(self):
        import live_drift
        self.assertEqual(d.MONITORING_CRDS, live_drift.MONITORING_CRDS)
        group, resource = "apiextensions.k8s.io", "customresourcedefinitions"
        self.assertEqual([rule for rule in ROLES[d.DEPLOYER, ""] if resource in rule["resources"]],
                         [{"apiGroups": [group], "resources": [resource], "resourceNames": list(d.MONITORING_CRDS),
                           "verbs": ["get"]}])
        for identity in d.IDENTITIES:
            for name in d.MONITORING_CRDS:
                self.assertTrue(d.allowed(identity, R("", group, resource, "get", name), ROLES))
            # check proves the cluster denies another CRD and the collection.
            for request in [R("", group, resource, "get", "certificates.cert-manager.io"),
                            R("", group, resource, "list", ""), R("", group, resource, "watch", "")]:
                with self.subTest(identity=identity, request=request):
                    self.assertFalse(d.allowed(identity, request, ROLES))
                    self.assertIn(request, d.denied(identity))
            for verb in ("patch", "update", "delete"):
                self.assertFalse(d.allowed(identity, R("", group, resource, verb, d.MONITORING_CRDS[0]), ROLES))

    def test_bindings_name_both_users_and_keep_the_second_role_for_the_manual_identity(self):
        items = d.bindings(MANUAL_USER, SCHEDULED_USER)["items"]
        self.assertEqual(len(items), len(ROLES))
        for item in items:
            namespace = item["metadata"].get("namespace", "")
            with self.subTest(binding=item["metadata"]):
                self.assertEqual(item["kind"], "RoleBinding" if namespace else "ClusterRoleBinding")
                self.assertEqual(item["roleRef"], {"apiGroup": d.RBAC, "kind": "Role" if namespace else "ClusterRole",
                                                   "name": item["metadata"]["name"]})
                self.assertIn((item["metadata"]["name"], namespace), ROLES)
                users = [subject["name"] for subject in item["subjects"]]
                self.assertEqual({subject["kind"] for subject in item["subjects"]}, {"User"})
                expected = [SCHEDULED_USER, MANUAL_USER] if item["metadata"]["name"] == d.DEPLOYER else [MANUAL_USER]
                self.assertEqual(users, expected)
        # Without a scheduled user, every binding names the reviewer-gated identity only.
        manual_only = d.bindings(MANUAL_USER)["items"]
        self.assertEqual(len(manual_only), len(ROLES))
        for item in manual_only:
            self.assertEqual([subject["name"] for subject in item["subjects"]], [MANUAL_USER])

    def test_bindings_reject_anything_but_two_distinct_user_ocids(self):
        for scheduled, manual in [(SCHEDULED_USER, SCHEDULED_USER), ("jkowall", MANUAL_USER),
                                  (SCHEDULED_USER, "ocid1.group.oc1..aaaa"), (SCHEDULED_USER, MANUAL_USER + "\n"),
                                  ("system:masters", MANUAL_USER), ("", MANUAL_USER)]:
            with self.subTest(scheduled=scheduled, manual=manual), self.assertRaises(ValueError):
                d.bindings(manual, scheduled)
        with self.assertRaises(ValueError):
            d.bindings(None)

    def test_no_tenancy_identifier_is_committed(self):
        for path in [d.ROLES, d.ROOT / "docs/deployer-access.md", d.ROOT / "scripts/demo/deployer_access.py"]:
            with self.subTest(path=path):
                self.assertIsNone(d.USER.search(path.read_text()))

    def test_review_carries_the_subresource_and_omits_empty_fields(self):
        self.assertEqual(d.review(MANUAL_USER, R("opensearch", "", "pods/exec", "create", ""))["spec"],
                         {"user": MANUAL_USER, "resourceAttributes": {
                             "namespace": "opensearch", "resource": "pods", "subresource": "exec", "verb": "create"}})
        self.assertEqual(d.review(MANUAL_USER, R("", d.RBAC, "clusterroles", "bind", "otel-collector"))["spec"],
                         {"user": MANUAL_USER, "resourceAttributes": {
                             "group": d.RBAC, "resource": "clusterroles", "verb": "bind", "name": "otel-collector"}})
        grouped = d.review(MANUAL_USER, R("", "", "nodes", "get", ""), groups=["ocid1.group.oc1..aaaa"])
        self.assertEqual(grouped["spec"]["groups"], ["ocid1.group.oc1..aaaa"])
        # A credential reviewing itself names no user, so nothing can be asked about another identity.
        own = d.review(None, R("opensearch", "", "pods/exec", "create", d.OPENSEARCH_POD))
        self.assertEqual((own["kind"], own["spec"]), ("SelfSubjectAccessReview", {"resourceAttributes": {
            "namespace": "opensearch", "resource": "pods", "subresource": "exec", "verb": "create",
            "name": d.OPENSEARCH_POD}}))

    def test_check_reports_missing_and_excess_live_access(self):
        def cluster(answer):
            def run(reviews):
                return [{"spec": review["spec"], "status": {"allowed": answer(review["spec"])}} for review in reviews]
            return run

        def committed(spec):
            return d.allowed("scheduled", request_of(spec), ROLES)

        self.assertEqual(d.check("scheduled", SCHEDULED_USER, run=cluster(committed)), [])
        # The deploy job's own preflight asks only for what it needs.
        asked = []
        self.assertEqual(d.check("scheduled", None, only_required=True,
                                 run=lambda reviews: asked.extend(reviews) or cluster(committed)(reviews)), [])
        self.assertEqual(len(asked), len(d.required("scheduled")))
        self.assertEqual({item["kind"] for item in asked}, {"SelfSubjectAccessReview"})
        # An identity that is a cluster administrator has every denied request.
        admin = d.check("scheduled", SCHEDULED_USER, run=cluster(lambda spec: True))
        self.assertEqual(admin, [(request, False) for request in d.denied("scheduled")])
        # The read-only identity of today lacks every required request.
        unbound = d.check("scheduled", SCHEDULED_USER, run=cluster(lambda spec: False))
        self.assertEqual(unbound, [(request, True) for request in d.required("scheduled")])
        with self.assertRaises(ValueError):
            d.check("scheduled", SCHEDULED_USER, run=lambda reviews: [])
        # A review the cluster could not evaluate, or answered out of order, is not a denial.
        for broken in [lambda r: {"spec": r["spec"]},
                       lambda r: {"spec": r["spec"], "status": {"allowed": False, "evaluationError": "webhook down"}},
                       lambda r: {"spec": {"resourceAttributes": {"verb": "other"}}, "status": {"allowed": False}}]:
            with self.subTest(broken=broken), self.assertRaises(ValueError):
                d.check("scheduled", SCHEDULED_USER, run=lambda reviews: [broken(r) for r in reviews])

    def test_reviews_go_through_kubectl_with_a_request_timeout_and_report_its_error(self):
        import subprocess
        calls = []

        def kubectl(status, stdout="", stderr=""):
            def run(args, **kwargs):
                calls.append((args, kwargs))
                return subprocess.CompletedProcess(args, status, stdout=stdout, stderr=stderr)
            return run

        reviews = [d.review(None, R("default", "", "pods", "list", ""))]
        answer = dict(reviews[0], status={"allowed": True})
        self.assertEqual(d.create(reviews, run=kubectl(0, json.dumps(answer))), [answer])
        args, kwargs = calls[0]
        self.assertEqual(["kubectl", "create", "--request-timeout=30s", "-f", "-", "-o", "json"], args)
        self.assertEqual(json.loads(kwargs["input"]), {"apiVersion": "v1", "kind": "List", "items": reviews})
        with self.assertRaisesRegex(ValueError, "could not complete the access reviews: error: You must be logged in"):
            d.create(reviews, run=kubectl(1, stderr="error: You must be logged in to the server (Unauthorized)\n"))
        with self.assertRaisesRegex(ValueError, "exit status 137"):
            d.create(reviews, run=kubectl(137))

    def test_kubectl_output_is_read_as_separate_documents_or_a_list(self):
        reviews = [{"kind": "SubjectAccessReview", "status": {"allowed": value}} for value in (True, False, True)]
        stream = "".join(json.dumps(item, indent=4) + "\n" for item in reviews)
        self.assertEqual(d.documents(stream), reviews)
        self.assertEqual(d.documents(json.dumps({"apiVersion": "v1", "kind": "List", "items": reviews})), reviews)
        self.assertEqual(d.documents(" \n"), [])
        with self.assertRaises(ValueError):
            d.documents(stream + "Error from server")

    def test_main_prints_bindings_and_rejects_a_bad_user(self):
        import io
        from unittest.mock import patch
        out = io.StringIO()
        with patch("sys.stdout", out):
            self.assertEqual(d.main(["bindings", "--scheduled-user", SCHEDULED_USER, "--manual-user", MANUAL_USER]), 0)
        self.assertEqual(json.loads(out.getvalue()), d.bindings(MANUAL_USER, SCHEDULED_USER))
        with self.assertRaises(ValueError):
            d.main(["check", "--identity", "scheduled", "--user", "someone"])


if __name__ == "__main__":
    unittest.main()
