<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Deployer access

[ops/rbac/deployer.yaml](../ops/rbac/deployer.yaml) is the Kubernetes access for the
two GitHub Actions identities that deploy the hosted demo. It is the reviewed
definition, not a record of what the cluster holds: an administrator applies it
and binds the identities, and records that in the release record. Every run of the
deploy, trace reset and volume expansion workflows starts by checking its own
credential against this role and stops before any change if part of it is
missing, so a run changes nothing until the role is applied and its identity bound.

The role replaces
[the access reviewed for the executor design](https://github.com/jaegertracing/jaeger-demo/blob/f9b54fe60b039633d00b5ea91dba6a40722e8fad/docs/all-five-schedule-access.md) and
the handover grants that fell short in
[#87](https://github.com/jaegertracing/jaeger-demo/issues/87).
[Executor retirement](executor-retirement.md) gives the order of that change, and
the [migration plan](MIGRATION.md#current-evidence-and-unresolved-gates) records
when the role was applied and the earlier grants were removed.

| Role | Bound to | Purpose |
| --- | --- | --- |
| `jaeger-demo-deployer` | `demo-apply` and `demo-scheduled` identities | Upgrade the six Helm releases and run their checks |
| `jaeger-demo-recovery` | `demo-apply` identity only | Manual OpenSearch volume expansion |

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
| `opensearch` | `pods/exec` on `opensearch-cluster-single-0`, read the data PersistentVolumeClaim | `verify_trace.py` version check, `deploy.py` binding check, `opensearch_reset.py` |
| `jaeger`, `opensearch`, `otel-demo` | Read Pod logs, endpoints, events and Certificates | Certificate readiness and failure diagnostics in `examples/otel-demo/deploy-all.sh` |
| `kube-system` | Get, patch and delete five named Services, list Services | Monitoring scrape Services, `kubectl cluster-info` |
| Cluster | Get, patch and delete seven named ClusterRoles (with `bind` and `escalate`) and seven named ClusterRoleBindings. Create a ClusterRole or ClusterRoleBinding of any name. Get, patch and delete the two named admission webhook configurations. Three namespace lookups. | Monitoring and OTel demo chart RBAC, monitoring hooks |
| Cluster | Get the monitoring chart's ten CustomResourceDefinitions (`*.monitoring.coreos.com`) by name. No list or watch, and no other CRD. | `live_drift.py` [drift report](helmfile-applications.md#live-drift-report) |

The verbs cover a release that Helm stored with either apply method. With
client-side apply Helm creates and patches; with server-side apply it creates by
patch.

The `jaeger-demo-recovery` role adds what volume expansion needs: in `opensearch`
it patches PersistentVolumeClaims, and at cluster scope it reads PersistentVolumes
and the `oci-bv` StorageClass. The scheduled identity never runs that script, so it
does not hold this role. The role kept its name when the backup and restore
tooling was removed ([#76](https://github.com/jaegertracing/jaeger-demo/issues/76)),
so its bindings did not have to change. It no longer creates a claim, a Pod or a
volume snapshot, and it no longer runs a command in a Pod of any name.

The manual [trace reset](releases.md#reset-the-trace-data) has no access of its
own. It uses the `pods/exec` grant on the OpenSearch Pod, the patch of the Jaeger
Deployment and the port-forward that the deployer role already holds, and it
deletes no Kubernetes object. The scheduled identity holds the same access. What
keeps a reset manual is the workflow (dispatch only, a `demo-apply` reviewer and a
typed acknowledgment), not the role.

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
- **Storage, `jaeger-demo-recovery` role only.** It can patch any
  PersistentVolumeClaim in `opensearch`. A patch can grow a volume, and a grown
  volume cannot be shrunk again.

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
- No direct create or delete of a PersistentVolumeClaim or a Pod, and no access
  to volume snapshots, snapshot classes or snapshot contents. That does not
  make stored data safe from a holder: it can change the OpenSearch StatefulSet
  and run commands in its Pod. Treat any change to that StatefulSet's volume claim
  settings as a storage change that needs explicit approval.
- No write to a Certificate. It reads Certificates in the three OTel namespaces,
  and it can replace the TLS Secrets there as described above.
- No change to its own ClusterRoles or ClusterRoleBindings. It can delete its
  RoleBindings in `default` and `opensearch`, which removes its own access there.
- No install of a release into a namespace that does not exist.

The workflow does not offer a clean reinstall. A clean run uninstalls what the
role can delete, ignores what it cannot, and cannot recreate the namespaces, so
under these identities it would leave the demo partly removed. The workflow has
no `deploy_mode` input and always runs `upgrade`, and `deploy.py` refuses `clean`
when `GITHUB_ACTIONS` is `true`. A clean reinstall deletes namespaces and
PersistentVolumeClaim bindings that the [migration plan](MIGRATION.md) preserves,
so only an administrator performs it, from a shell, with that administrator's own
kubeconfig and with explicit maintainer approval. It discards the stored traces:
generated traces are disposable and no backup tooling exists. Run
`./deploy-all.sh clean` in the stack's example directory (`examples/oci` or
`examples/otel-demo`) from a checkout of the reviewed commit. Pass the image
tag as the second argument or in `JAEGER_DEMO_IMAGE_TAG`; the repositories default
to the release images and `JAEGER_DEMO_JAEGER_IMAGE_REPOSITORY` and
`JAEGER_DEMO_HOTROD_IMAGE_REPOSITORY` override them. Running `scripts/demo/deploy.py` with
`JAEGER_DEMO_DEPLOY_MODE=clean` also works from a shell, but it first resolves
snapshot image tags and so needs `MAIN_SHA`, the `jaegertracing/jaeger` commit to
deploy.

## Where the workflows use the API key

Each identity is an OCI user whose API signing key is a GitHub environment secret.
The kubeconfig the workflows create holds no credential. `kubectl` and Helm get
a short-lived token by running `oci ce cluster generate-token`, and the OCI CLI
reads the key from the five `OCI_CLI_*` environment variables. A step therefore
needs the secrets when it configures cluster access or calls the cluster, and no
other step does.

The writer workflows set the five secrets in the `env` of those steps, not
for the job:

| Workflow | Steps that receive the secrets |
| --- | --- |
| `ci-deploy-demo.yml` | Configure kubectl with OKE, Verify the deployment identity's access, Reconcile demo components |
| `ci-opensearch-reset.yml` | Configure kubectl with OKE, Verify the deployment identity's access, List or reset the Jaeger trace indices |
| `ci-opensearch-volume-expand.yml` | Configure kubectl with OKE, Verify the deployment identity's access, Expand and verify the OpenSearch volume |

The checkout, the revision check, the OCI CLI installation
and version check, the three Helmfile preparation steps (toolchain, charts and
images), the Helm setup action, the deployment record upload and the Slack
notification run without them. The only action that receives them is the kubectl configuration,
`oracle-actions/configure-kubectl-oke`, pinned by commit.
`scripts/demo/workflow_test.py` fails if a secret appears at workflow or job
level, in an action input or a script, under another name, as part of the set, or
on a step that runs any other action, and if the steps in this table change. The
five lines are repeated in each step. GitHub Actions accepts YAML anchors, but
not the merge key that would combine an anchor with a step's other variables.

This limits which processes are handed the key. It does not isolate the steps of
a job from each other: they share one runner, so an earlier step that was
compromised could replace a tool that a later step runs with the key. Keep every
action pinned by commit, and review a new step in these workflows as a change to
who can use the identity.

### OCI CLI installation

The OCI CLI comes from PyPI on every run.
[oci-cli-hashes.txt](../scripts/demo/oci-cli-hashes.txt) lists each pin of
[oci-cli-constraints.txt](../scripts/demo/oci-cli-constraints.txt) with the
SHA-256 of every file PyPI publishes for that release. The step **Install the
hash-verified OCI CLI** runs before any step holds the key:

```bash
python -m pip install --require-hashes --only-binary :all: --no-binary PyYAML -r scripts/demo/oci-cli-hashes.txt
```

pip refuses a file whose hash is not listed and a dependency that is not listed,
so a changed file, or one added to a release later, stops the run. The kubectl
configuration action then runs its own `python -m pip install oci-cli`, which
takes no options. That step sets `PIP_NO_INDEX`, so the command succeeds only
when nothing is left to install, and it cannot download a package while the key
is in its environment. `verify_oci_cli.py` then checks every installed version.
The `Demo tests` CI job runs the same three commands without credentials.

Hash checking does not cover the following:

- **The PyYAML build.** OCI CLI 3.96.3 accepts no PyYAML later than 6.0.2, and
  that release has no wheel for Python 3.14, the hosted runner's version when
  this was written. pip verifies the source archive, then builds it with the
  setuptools, wheel and Cython releases PyPI serves at that moment. pip checks
  neither hashes nor pins for build requirements. `--only-binary :all: --no-binary
  PyYAML` keeps this the only source build. The build runs in the install step,
  without the key, on the runner the later steps use. When a pinned OCI CLI
  accepts a PyYAML release that has a wheel, remove `--no-binary PyYAML` from the
  workflows that install the OCI CLI.
- **Packages from the runner image.** pip does not download a pin that the image
  already provides at that version, so only the version of such a package is
  checked.
- **Python before 3.13.** Three pins need `typing_extensions` there. The files
  do not pin it, so the install stops.
- **kubectl.** The pinned `oracle-actions/configure-kubectl-oke` action downloads
  `kubectl` from `dl.k8s.io` without checking a checksum, inside the step that
  holds the key, and the later steps that hold the key run that binary. The
  OCI CLI hashes do not cover it.
- **The origin of the hashes.** They are what PyPI recorded when
  `scripts/utils/update-oci-cli-hashes.py` ran. They detect a later change. They
  do not show that a release was sound when it was pinned.

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

Once both identities are bound, pass both users every time. Applying the output
for the manual user alone replaces the subjects of the six deployer bindings,
which removes the scheduled identity, and the next scheduled run stops at its
access check.

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
list. The deploy, trace reset and volume expansion workflows run it as their first
cluster step.

Record the reviewed commit, the command outputs and the time in the release
record. The first manual upgrade of each component through **Deploy Jaeger Demo to
OKE** is the acceptance of the role, starting with `oci` / `monitoring`, which is
the upgrade that failed in #87.

### Tested against a real API server

The `deployer-access` job in [ci.yml](../.github/workflows/ci.yml) runs
[the deployer access rehearsal](../scripts/demo/rehearsals/deployer-access/exercise.py)
on every pull request and push to `main`, on a disposable kind cluster at the
Kubernetes version of the toolchain lock. The cluster's API server writes an
audit log ([kind.yaml](../scripts/demo/rehearsals/deployer-access/kind.yaml),
[audit-policy.yaml](../scripts/demo/rehearsals/deployer-access/audit-policy.yaml)).
It uses no production credential. As the cluster administrator it:

1. creates the `jaeger`, `opensearch` and `otel-demo` namespaces and applies
   `ops/rbac/deployer.yaml`, first with `--dry-run=server`, so the API server
   accepts every rule;
2. binds a fixture manual user and a fixture scheduled user with the `bindings`
   output, reads each user's groups from `kubectl auth whoami` under a
   kubeconfig that impersonates it, and requires `check --user` with those
   groups to report `0 difference(s)` for both identities, required and denied
   requests alike, so a grant to a group such as `system:authenticated` counts;
3. under each impersonating kubeconfig, requires `check --self` to report
   `0 difference(s)` and Helm to be refused as that user;
4. removes the rule for the five named `kube-system` Services and requires
   `check` to report exactly those requests as `missing`, then grants an extra
   `get` on `kube-system` Secrets and requires it as `must not be allowed`;
5. installs `default/prometheus` and applies `prometheus-svc.yaml` as the base of
   the change has them: on a pull request, the base commit's chart pin, values,
   flags and Services, with the chart verified against the base's lock;
   otherwise, or when the base deploys the same monitoring inputs or cannot seed
   the release, this checkout's, and the job log says the base to head
   transition was not exercised. It installs by client-side apply.

Then the scheduled user, holding only this role, runs this checkout's
`examples/oci/deploy-all.sh upgrade` with the environment `deploy.py` gives a
scheduled `oci` / `monitoring` run, public smoke checks off. That is Helm's
default `helm upgrade --wait` with the chart's hooks. The administrator returns
the release to the base by server-side apply, and the scheduled user upgrades it
again. Production may hold the release either way, and the two differ: with
client-side apply Helm creates each hook object by a create request, which needs
the `escalate` rule with the empty resource name; with server-side apply it
creates them by an apply patch on the named object. The four revisions must end
`deployed`, with apply methods client, client, server, server. The audit log
must show no request refused to the scheduled user during either upgrade, so a
refusal that Helm logs and then ignores fails the job too.

`prometheus-svc.yaml` is already applied when the scheduled user runs, as in
production. Its `kubectl apply` patches the two Services only when the pull
request changes the file; otherwise it reads them and changes nothing.

A probe release then shows that the check and the upgrade fail together: with
the `default` Ingress rule removed, `check` reports exactly that rule's requests
as `missing`, and the scheduled user's upgrade that adds an Ingress fails with a
refusal naming that user, `get`, `ingresses` and `default`, before any change to
the release. With the committed role back, `check` reports `0 difference(s)` and
the same upgrade succeeds.

Last, as the scheduled user, Helm's install into `kube-system` and its upgrade
that adds a ConfigMap there, both with `--dry-run=server`, must stop at Helm's
check of whether the new object exists: the API server refuses that user `get`
on ConfigMaps in `kube-system`. The test matches the refusal on the user, verb,
resource and namespace, and the release history must not change. This shows that
the role does not let Helm start such a change. It does not run one.

The rehearsal runs `deploy-all.sh`, not `deploy.py`. What `deploy.py` itself
calls, such as its `helm status` checks and the revision ConfigMap, is covered
only by the access reviews of `check`. The `operations` job runs the Helmfile
engine, `helmfile_deploy.py`, on its own kind cluster as a user bound to this role:
`helm list` per namespace, `helm history`, the per-release Helmfile apply and sync,
the companion object reads and first install, the generator restarts, the
fingerprint patches, the live image reads through `kubectl proxy`, and the
rollout, OpenSearch claim and Certificate checks, on synthetic application charts,
fixture workloads and the real `demo-resources` chart. Only the registry lookup,
the public health checks and the trace write are stubbed there. The job also does not prove the
production cluster: it shows nothing about the bindings an administrator
applied, OCI IAM policies, or group grants outside Kubernetes. The production
check above still applies.

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
`kubectl` or `helm` call in a script needs a row in the module's table. The
`deployer-access` CI job then applies the changed file to a real API server and
runs the scheduled monitoring upgrade under it
([tested against a real API server](#tested-against-a-real-api-server)), so a
rule the server rejects, or a monitoring upgrade the role cannot complete, fails
the pull request. After the change merges, an administrator applies the file again and
reruns `check`. A merge alone does not change the cluster.

The order depends on the direction of the change:

- **A change that adds access.** Until the file is applied, each run stops at
  **Verify the deployment identity's access** with `missing` lines, before any
  change. Merge after the 13:00 UTC run or pause the schedule, apply the file,
  and require `0 difference(s)` before the next run.
- **A change that only removes access.** Runs keep passing while the cluster
  still holds the older, wider role, because a run's own `check --self` asks only
  for what it needs. Nothing has to be paused. The administrator's `check --user`
  reports each removed access as `must not be allowed` until the file is applied,
  when it is on the denied list. Apply the file when no writer workflow is
  running, then require `0 difference(s)` for both identities.

`kubectl apply -f ops/rbac/deployer.yaml` replaces the rules of each role, so it
also removes access. It does not touch the bindings, which name the roles and stay
valid as long as the role names do.

To withdraw the access, delete the bindings:

```bash
python3 scripts/demo/deployer_access.py bindings --manual-user "$MANUAL_USER_OCID" --scheduled-user "$SCHEDULED_USER_OCID" | kubectl delete -f -
```
