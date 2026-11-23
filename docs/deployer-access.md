<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Deployer access

[ops/rbac/deployer.yaml](../ops/rbac/deployer.yaml) is the Kubernetes access for the
two GitHub Actions identities that deploy the hosted demo. It is the reviewed
definition, not a record of what the cluster holds: an administrator applies it
and binds the identities, and records that in the release record. Until then the
identities keep the access described in
[the access reviewed for the executor design](https://github.com/jaegertracing/jaeger-demo/blob/f9b54fe60b039633d00b5ea91dba6a40722e8fad/docs/all-five-schedule-access.md) and
[#87](https://github.com/jaegertracing/jaeger-demo/issues/87). Every run of the
deploy, recovery and volume expansion workflows starts by checking its own
credential against this role and stops before any change if part of it is
missing, so apply the role before the first run after
[#95](https://github.com/jaegertracing/jaeger-demo/issues/95) merges.
[Executor retirement](executor-retirement.md) gives the order.

| Role | Bound to | Purpose |
| --- | --- | --- |
| `jaeger-demo-deployer` | `demo-apply` identity now; `demo-scheduled` identity once the schedule deploys directly | Upgrade the six Helm releases and run their checks |
| `jaeger-demo-recovery` | `demo-apply` identity only | Manual OpenSearch recovery test and volume expansion |

Each role is one ClusterRole for kinds that have no namespace, plus one Role per
namespace. A Role grants nothing outside its namespace. Rules in the four release
namespaces cover a kind, because charts add and rename objects. Rules anywhere
else name each object, except where Kubernetes cannot limit a create, list or
watch by name.

## What the deployer role grants

| Scope | Access | Needed by |
| --- | --- | --- |
| `default`, `jaeger`, `opensearch`, `otel-demo` | Create, read, patch and delete the kinds the charts render there. Read, create, update and delete Secrets. Read Pods and ReplicaSets. | `helm upgrade --wait`, `kubectl apply`, rollout checks |
| `default` | Roles and RoleBindings, monitoring custom resources, hook Jobs | `default/prometheus` and its hooks |
| `jaeger` | `pods/portforward` | `verify_trace.py` writes and reads one trace |
| `opensearch` | `pods/exec` on `opensearch-cluster-single-0`, read the data PersistentVolumeClaim | `verify_trace.py` version check, `deploy.py` binding check |
| `jaeger`, `opensearch`, `otel-demo` | Read Pod logs, endpoints, events and Certificates | Certificate readiness and failure diagnostics in `examples/otel-demo/deploy-all.sh` |
| `kube-system` | Get, patch and delete five named Services, list Services | Monitoring scrape Services, `kubectl cluster-info` |
| Cluster | Get, patch and delete seven named ClusterRoles (with `bind` and `escalate`) and seven named ClusterRoleBindings. Create a ClusterRole or ClusterRoleBinding of any name. Get, patch and delete the two named admission webhook configurations. Three namespace lookups. | Monitoring and OTel demo chart RBAC, monitoring hooks |

The verbs cover a release that Helm stored with either apply method. With
client-side apply Helm creates and patches; with server-side apply it creates by
patch.

The recovery role adds, in `opensearch`: create and patch PersistentVolumeClaims,
create and delete Pods, run a command in any Pod, and create and annotate volume
snapshots. At cluster scope it reads PersistentVolumes, the `oci-bv` StorageClass
and the recovery snapshot class, creates snapshot classes, and reads and patches
volume snapshot contents. The scheduled identity never runs those scripts, so it
does not hold this role.

## What a holder can reach

The role is scoped to what the deployment does. It is not a strong boundary
against someone who controls the identity:

- **Cluster RBAC.** `escalate` lets it rewrite the rules of the seven named
  ClusterRoles and create a ClusterRole of any name with any rules. `bind` and
  unrestricted ClusterRoleBinding creation let it bind a named role to any
  subject. Helm needs these to upgrade the monitoring chart.
- **Admission.** It can patch the monitoring chart's two webhook configurations,
  which can observe, change or block API requests in any namespace.
- **Workloads.** It can run any workload under any service account in the four
  release namespaces. The Prometheus operator's service account reads and writes
  Secrets cluster-wide. `default` admits the privileged node exporter, so a
  changed DaemonSet there runs as root on every node.
- **Secrets.** It reads and replaces every Secret in the four release namespaces,
  including TLS keys and Helm release records. A rule cannot select release records
  by name prefix. A replaced release record changes what a later rollback installs.
- **Namespaced RBAC.** In `default` it creates and changes Roles and RoleBindings
  with any subject. In `opensearch` it creates and changes RoleBindings.
- **Routing and certificates.** It changes Services and Ingresses in the release
  namespaces, including `default/kubernetes`, and the selectors and ports of the
  five `kube-system` scrape Services. An Ingress it creates can claim a hostname
  on the shared ingress controller and, with the issuer annotation the demo
  already uses, have cert-manager issue a certificate for it.
- **Pods.** `pods/exec` runs any command in the OpenSearch Pod, which can delete
  its indices. `pods/portforward` reaches every Pod in `jaeger`.
- **Storage, recovery role only.** It can patch any volume snapshot content in the
  cluster, including one that belongs to another workload, and run a command in
  any Pod in `opensearch`.

A hostile holder can therefore reach cluster-administrator access. The control is
who can use the identity: code-owner review and merge to `main`, a `main`-only
branch policy on both GitHub environments, and reviewer approval of each
`demo-apply` run. Those are GitHub settings. This repository cannot show them, so
verify them before binding an identity. The narrow rules protect against
mistakes, not against that actor.

## What it does not grant

These hold for the rules as written. They are limits on mistakes, and the previous
section explains how a hostile holder gets past them.

- No access in the ingress controller or cert-manager namespaces.
- In `kube-system`, it reads every Service and changes or deletes only the five
  named ones. It reads nothing else there.
- No create, patch or delete of a Namespace, Node, CustomResourceDefinition,
  PersistentVolume, admission policy or ClusterIssuer.
- No direct delete of a PersistentVolumeClaim or a volume snapshot. That does not
  make stored data safe from a holder: it can change the OpenSearch StatefulSet
  and run commands in its Pod. Treat any change to that StatefulSet's volume claim
  settings as a storage change that needs explicit approval.
- No write to a Certificate. It reads Certificates in the three OTel namespaces,
  and it can replace the TLS Secrets there as described above.
- No change to its own ClusterRoles or ClusterRoleBindings. It can delete its
  RoleBindings in `default` and `opensearch`, which removes its own access there.
- No install of a release into a namespace that does not exist.

`deploy_mode=clean` is not safe with these identities. A clean run uninstalls what
the role can delete, ignores what it cannot, and cannot recreate the namespaces,
so it leaves the demo partly removed. Do not dispatch it; a clean reinstall is an
administrator action.

## Apply and bind

An administrator runs these from a checkout of the reviewed commit, with a
kubeconfig for the demo cluster. The module needs Python 3 with PyYAML. Use the
OCI user OCID of each identity; the repository never records them.

```bash
kubectl apply -f ops/rbac/deployer.yaml
```

Bind the reviewer-gated identity first:

```bash
python3 scripts/demo/deployer_access.py bindings --manual-user "$MANUAL_USER_OCID" | kubectl apply -f -
```

Bind the scheduled identity only after the manual upgrades are accepted and just
before the schedule is enabled
([executor retirement](executor-retirement.md#8-bind-the-scheduled-identity)).
Until then nothing uses that access, and an unused grant on a credential that
needs no reviewer is only risk. The same command with both users adds it:

```bash
python3 scripts/demo/deployer_access.py bindings --manual-user "$MANUAL_USER_OCID" --scheduled-user "$SCHEDULED_USER_OCID" | kubectl apply -f -
```

The command prints one ClusterRoleBinding or RoleBinding per role and refuses
anything that is not an OCI user OCID, or the same user twice. Kubernetes accepts
the empty resource name in the `escalate` rules; confirm with
`kubectl apply --dry-run=server -f ops/rbac/deployer.yaml` before the first apply.

## Verify the live access

`check` asks the cluster, with one SubjectAccessReview per request, whether an
identity has every access the deployment needs and none of a list of accesses it
must not have. A review changes nothing in the cluster.

```bash
python3 scripts/demo/deployer_access.py check --identity manual --user "$MANUAL_USER_OCID"
```

```bash
python3 scripts/demo/deployer_access.py check --identity scheduled --user "$SCHEDULED_USER_OCID"
```

Each command must report `0 difference(s)`. It fails, instead of reporting a
result, if the cluster cannot evaluate a review.

- `missing` means a binding was not applied, or the role is older than the
  checkout.
- `must not be allowed` means the identity holds access from somewhere else: an
  earlier binding that was not retired, or an OCI IAM policy that makes the user a
  cluster administrator outside Kubernetes RBAC. Find and remove that grant before
  relying on this role.

An administrator's review names the user only. Access the identity gets through
a group is not evaluated unless each group is passed with `--group`, so a clean
result does not rule out a group grant. `check --identity <name> --self` closes
that gap for the required access: run with the identity's own credential, it
reviews what that credential can really do, and asks nothing about the denied
list. The deploy, recovery and volume expansion workflows run it as their first
cluster step.

Record the reviewed commit, the command outputs and the time in the release
record. The first manual upgrade of each component through **Deploy Jaeger Demo to
OKE** is the acceptance of the role, starting with `oci` / `monitoring`, which is
the upgrade that failed in #87.

## Changing the role

`scripts/demo/deployer_access.py` builds the required access from two sources: the
object inventory in [chart-objects.json](../ops/rbac/chart-objects.json), and
a table in the module of the commands the scripts run beyond those objects.
`scripts/demo/deployer_access_test.py` fails in both directions:

- A chart object or listed command that needs access the role lacks fails with
  the missing requests.
- A rule that no chart object or listed command explains fails as unexplained.

The inventory is kept equal to the pinned charts by
`python3 scripts/demo/chart_objects.py --check`, which the `Helmfile render and
adoption rehearsal` job runs on every pull request. It renders the six releases
the way the scripts install them, without cluster access, and compares object
identities and hook events; the chart archive list in the inventory's header is
not part of that comparison. It fails when a rendered object has no record.
`--write` adds the missing records and corrects hook events. It never removes a
record that no pinned chart renders any more, because Helm deletes that object on
the next upgrade and needs the access to do so; remove the record by hand once no
release needs it. So a chart
bump that adds a kind, a hook or a cluster-scoped object changes the inventory and
the role in the same pull request, and the reviewer sees the access change. A new
`kubectl` or `helm` call in a script needs a row in the module's table. After the
change merges, an administrator applies the file again and reruns `check`. A merge
alone does not change the cluster.

To withdraw the access, delete the bindings:

```bash
python3 scripts/demo/deployer_access.py bindings --manual-user "$MANUAL_USER_OCID" --scheduled-user "$SCHEDULED_USER_OCID" | kubectl delete -f -
```
