<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Daily executor permissions

The [offline generator](../ops/executor/permissions.py) renders candidate Kubernetes permissions. The workload identities are `jaeger-demo-operations/jaeger-demo-preparer` and `jaeger-demo-operations/jaeger-demo-executor`. Every output is written to a new mode-0600 local file. Installing roles, binding principals, enabling the pods and enabling scheduling are separate production access decisions. [Deployment inventory](all-five-schedule-access.md) · [Request protocol](daily-update-request.md)

| Stage | Scope |
| --- | --- |
| `reads` | Named GETs from the six-release matrix, ten CRDs, the monitored webhook Secret, named OpenSearch Pod/PVC, the cluster namespace and version. Helm history requires both identities to GET and LIST Secrets in four namespaces. This exposes other credentials in these namespaces. The executor additionally receives namespaced workload readiness reads. |
| `routine-guards` | Requires a running scripts-v1 request and its checkpoint lock. The two Jaeger releases may update four named containers to a full source commit plus registry digest in the approved upstream repositories. The two generators may update their restart timestamp to the request's UTC slot. Other existing listed chart resources must preserve their complete content. |
| `routine-grants` | Grants only named PATCH on those existing inventory objects and the named checkpoint. Both account names and namespaces are fixed. |
| `history-guard` | Limits the executor's Secret lifecycle to canonical Helm records for the six releases under a running daily request and holder. A newly created record must be pending upgrade or rollback. Existing chart Secret payloads must remain unchanged. |
| `history-grants` | Grants Helm's Secret CREATE/UPDATE/DELETE in the four release namespaces. Kubernetes cannot express a name constraint for CREATE in RBAC. Install and directly test the native guard before binding any of these permissions. |
| `workload-guards`, `workload-grants` | Optional slot-specific state approval for a named ordinary resource. Both its old and new admitted states must be independently approved at the same UID and the exact configuration/bundle/slot. |

For example, `python3 ops/executor/permissions.py routine-guards --output "$NEW_GUARDS_FILE"` only renders the manifest. Review the output, install guards before grants, and send negative requests under the real SA before making any writer available. Remove grants before removing their corresponding guards. The separately rendered control-record policy protects request state transitions. Coordinate the independently generated privileged hook authorities separately.

Each admission binding denies requests when its parameter is missing. Before allowing the request to enter running, the executor must have acquired its exact dated lock including the request ConfigMap UID. While it is running, an independent policy prevents any identity from replacing or deleting that holder. A per-mutation binding also checks the active checkpoint. The executor can change at most one of the five 64-hex component fingerprints at a time under its lock and release the holder only after verification. Ordinary content-neutral checks preserve API-visible data, pod specs, UID, labels, annotations, finalizers and owner references. They ignore server-accounted managed fields, resource version and generation. Snapshot changes preserve all other container and pod fields. The executor separately checks the independently frozen signed image digests and actual serving Pods.

The optional offline state-review file contains `slot`, `configurationSHA`, `bundleSHA256` and `changes`. Each entry contains `namespace` (or `cluster`), API `group`, plural `resource`, `name` and one to three complete API-defaulted `states` for the same object. Render both routine stages with the same `--review` to replace their generic content checks for that object, then install its exact guards before updating grants. Inspect these private outputs locally. A literal policy exposes the non-Secret fields it contains to admission-policy readers. Exclude credentials and operationally sensitive data. The generator refuses raw Secret payloads, hooks, arbitrary resource identities and unbounded policies.

**Helm history trust.** Native CEL verifies the outer Secret identity, type, metadata and the existence of exactly one release payload. The serialized Helm payload is compressed. A trusted independent executor verifies actual candidate and successful rollback manifests against signed custody before use. Native admission cannot authenticate that compressed content. Before manually reusing a revision touched by a potentially compromised writer, independently decode it and verify its chart, values, resources, hooks and approved digests against trusted custody. Treat unverifiable revisions as an incident. An additional independently operated admission webhook could decode bounded Helm records and compare the candidate and rollback manifests against independently signed plan digests before accepting the Secret.

The [native API rehearsal](../ops/executor/rehearsals/permissions.py) uses two short-lived ServiceAccount tokens on a fresh loopback Kubernetes API. It exercises both snapshot stacks, slot-bound restarts, holder/fingerprint transitions, preserved chart data, an exact approved ordinary Deployment change and the Secret history boundary. The native policy and raw Kubernetes grants require no cloud administrator token in either runtime. Confirm the actual OKE version, API support, existing aggregate grants, rollout API accesses and all protected chart/rollback contents before production activation.

## Daily parameters for reviewed changes

The installed daily path accepts the existing six Helm releases across all five
components. For each selected release the independent preparer reads the
specific live UID, the frozen history hashes and the reviewed render. When a
chart manifest differs, it submits the proposed operation with server-side
dry-run. The Kubernetes API runs its usual defaulting and admission. The
preparer records the existing and returned states for that UID in one of five
private DailyWorkloadApproval resources. Unchanged chart objects retain the
ordinary no-change path. Trace-generator changes use the exact same JSON Patch
builder as the writer. Application images are bound to the exact admitted digest
in the dated projection.

For Helm 4.3.2 server-side apply, the preparer sends an existing-object
apply-patch with manager `helm`, force disabled, strict field validation and
`dryRun=All`. It uses the projected service-account certificate and token at
the fixed Kubernetes API endpoint. For client-side apply, the installed
`/usr/local/bin/jaeger-helm-preview` computes the three-way strategic patch
with the versions used by Helm, then Kubernetes validates the patch in dry-run.
Custom resources use Helm's two-way JSON merge behavior for upgrades performed
without changing ownership. Both modes preserve the stored Helm apply method.
The local program accepts objects over stdin and suppresses private error
details. Its Go dependencies are pinned in `ops/executor/preview/go.mod` and
verified with `go.sum`.

Install stages in this order on a deployment approved for the service accounts.
The operator creates the CRD with `approval-schema`, waits for its Established
condition, seeds the five fixed names with `approval-seeds`, then installs
`approval-guards`, `daily-guards` and the separate history and hook guards.
Verify the bindings and their rejection cases before installing the
`approval-grants`, `daily-grants`, read, hook and history roles. The
`daily-guards` stage includes the ordinary content guards. It replaces
`routine-guards` for this installation. Existing installations must reconcile
the rendered policies and bindings by name and verify that no stale overlapping
policy blocks the new path. Each stage is rendered locally by
`python3 ops/executor/permissions.py STAGE --output NEW_FILE`.
The daily operation reuses the same CRD, admission policies and roles for each
slot.

The preparer can read and patch only the five named private projection
resources in the operations namespace. Its listed workload patch rights
require Kubernetes `request.dryRun`. The API denies an actual update. It cannot
create or delete workloads. The executor may change an existing named ordinary
object from one exact staged variant to another while holding the protected
daily lock. UID, mutable metadata and object fields must match. Changes to
controller-updated status do not authorize a change on the status subresource.
The snapshot, ordinary and checkpoint guards run alongside the independent
controls for Helm history and monitoring hooks.

The preparer authenticates every projection UID and complete spec using an
aggregate SHA-256 in the private signed daily index. Staging completes before
the signed index and the ready request appear. Startup can verify a finished
index through API reads alone. A failed partial stage can be replaced before
advertisement. The ready, requested and running transitions each require every
native parameter to match the control object's UID, date, configuration commit
and sealed bundle digest. A running or failed slot blocks replacement. An
expired, unclaimed slot can be superseded after the trusted preparer checks
expiry. The requester needs access only to its existing request ConfigMap.
Each private partition is limited to 100 changed identities and 640 KiB of
spec. A larger change needs operator handling before any daily mutation.
Keep read access private because chart configuration and pod metadata can
contain internal values. Kubernetes Secret bodies, cluster-scoped security
resources, monitoring operator pods and hooks are excluded from these
parameters.

Service routing, OpenSearch storage identity, the workload service account,
injected credentials, volumes and pod privileges stay bound to the existing
configuration. The cluster-reading OTel Collector agent additionally requires
the same pod settings and commands with same-repository digest-pinned image
changes. An attempted change across one of these boundaries aborts
preparation. A previously failed release uses Helm's actual existing original
manifest for preparing a new upgrade. A failure that needs a different
admitted rollback state returns to operator recovery under the existing
verified-history procedure.

The disposable loopback rehearsal is in
`ops/executor/rehearsals/approvals.py`. Supply a fresh Kubernetes API server
with RBAC and ValidatingAdmissionPolicy enabled, a localhost kubeconfig context
named `fixture`, Helm 4.3.2 and the locally built `jaeger-helm-preview` on
PATH. It mints separate short-lived preparer and executor tokens and exercises
the matrix's actual ConfigMap names for all five components. Its two real Helm
Deployment transactions use a synthetic chart in each existing Jaeger
namespace, one CSA and one SSA. The check compares the stored upgrade with the
independent preview and verifies the original UID and contents after real
rollback. The rehearsal also checks denial of partial and altered projections,
out-of-scope Pod and ReplicaSet reads, workload writes by the preparer, and
unapproved ordinary mutations.

## Monitoring hook identities

`monitoring_hooks.py` verifies the public monitoring chart archive against the repository checksum, renders the twelve hook objects and asks the explicitly selected API server to default each object using a nonpersistent CREATE. Run this from the reviewed checkout and an independently verified operator context while the hook objects are absent. The two output files hold the candidate rules and grants for inspection.

```bash
python3 ops/executor/monitoring_hooks.py --archive "$monitoring_chart" \
  --kubeconfig "$operator_kubeconfig" --context "$context" \
  --cluster-uid "$verified_kube_system_uid" --output "$new_review_directory"
```

The generated package grants the executor CREATE for seven resource kinds used by monitoring hooks, named GET and DELETE for twelve objects, and named `bind` for the chart's Roles and ClusterRoles. Kubernetes authorizes role creation against the collection URL. Creating the three pinned Role and ClusterRole definitions uses `escalate` with the single empty resource name for the collection request on Roles in `default` and cluster-scoped ClusterRoles. Native authorization tests also deny `escalate` on existing named Roles and ClusterRoles. The independent admission policies restrict every CREATE to the pinned names and server-defaulted contents and preserve existing RBAC rules and aggregation on UPDATE. The runtime cannot administer these policies. Review the full effective grants for both service accounts before approval. [Kubernetes RBAC escalation and binding](https://kubernetes.io/docs/reference/access-authn-authz/rbac/#restrictions-on-role-creation-or-update)

The immutable archive ConfigMap in the operations namespace holds the published CRD bytes for native API comparison. The rendered file exceeds the size available for kubectl's client-side last-applied annotation. Use server-side creation or apply for the reviewed guard package. Compare each policy, binding and archive with its reviewed definition before granting the role bindings. Keep Helm and every manual writer idle during the installation. A missing parameter, unreviewed name or changed hook definition must deny the operation. Remove the grants before dismantling the guards.

When a daily operation owns the checkpoint, the CRD hook account can reapply the ten existing unchanged CRD specifications. The certificate hook can preserve the existing admission webhook configuration and read its existing Secret. Changes to the certificate, the webhook targets or the CRD specifications require an operator-led baseline change. The checkpoint rule allows separate manual-chart operations to use the previously approved operator procedures.

On the disposable Kubernetes API the executor created and deleted all twelve real hook definitions. Altered Job images, RBAC rules and bindings, the CRD archive, a foreign name and an inactive request were rejected. Tests also used the chart's individual service accounts to verify CRD, admission-webhook and certificate constraints during a daily operation. The API-only test leaves Jobs inert. The earlier kind monitoring exercise covers hook execution and rollback. [Hook rehearsal](../scripts/demo/rehearsals/helm-history/monitoring_hook_access.py) · [Workload rehearsal](../scripts/demo/rehearsals/monitoring-writer/README.md)
