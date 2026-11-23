<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Retiring the in-cluster executor

The daily update runs `scripts/demo/deploy.py` directly from the deploy workflow
([#95](https://github.com/jaegertracing/jaeger-demo/issues/95)). This runbook moves
production from the [in-cluster executor](https://github.com/jaegertracing/jaeger-demo/blob/f9b54fe60b039633d00b5ea91dba6a40722e8fad/docs/in-cluster-executor.md) to that
path and then removes what the executor design installed in the cluster. The
executor's code, manifests and documents are removed from `main`; they stay
readable at commit
[`f9b54fe`](https://github.com/jaegertracing/jaeger-demo/tree/f9b54fe60b039633d00b5ea91dba6a40722e8fad)
for anyone who needs to see what was installed.

A cluster administrator runs every cluster command here from a shell with a
kubeconfig for the demo cluster. No workflow identity can run them, and none
should: deleting admission policies and a namespace is outside the
[deployer role](deployer-access.md). Each step names its approval. Record each
step in the release record as it is done. Nothing in this document states that a
step has happened.

## The rule that orders everything

Most of the executor's admission policy bindings fail closed. They use
`failurePolicy: Fail` with `parameterNotFoundAction: Deny`, and their policies
match ConfigMaps, Secrets and workloads in every namespace. Their parameters are
the request record `jaeger-demo-operations/daily-hosted-update`, the
`DailyWorkloadApproval` objects and the deployment-revisions ConfigMap.

If a parameter disappears while a binding still refers to it, the API server can
deny ConfigMap, Secret and workload writes across the cluster, including Helm
release records. The public smoke check would not see that. So:

1. Delete every binding and policy first.
2. Delete the parameters, the namespace and the CRD only after that.
3. Never delete `default/jaeger-demo-deployment-revisions`. The deployment still
   uses it.

`jaeger-demo-immutable-approval` also denies deleting the approval objects for
every user, so a namespace delete attempted before the policies are gone stays in
`Terminating`.

While the parameters exist, the policies do not block the direct path, with two
exceptions to check in step 3: a request record in state `running`, and a lock
holder that starts with `daily/`.

## Before the first direct run

| # | Step | Approval |
| --- | --- | --- |
| 1 | Pause the schedule | none |
| 2 | Capture evidence and suspend the preparer | none |
| 3 | Read the request record and the lock | recovery only |
| 4 | Stop the executor | none |
| 5 | Clear a stale lock holder | yes |
| 6 | Apply the deployer role and bind the manual identity | yes, access change |
| 7 | Upgrade each component manually | reviewer of each run |
| 8 | Bind the scheduled identity | yes, access change |
| 9 | Enable the schedule | yes, activation |
| 10 | Accept the first scheduled run | yes, acceptance |

### 1. Pause the schedule

```bash
gh variable set DEMO_SCHEDULE_ENABLED --repo jaegertracing/jaeger-demo --body false
```

Confirm with `gh variable get DEMO_SCHEDULE_ENABLED --repo jaegertracing/jaeger-demo`
and that no **Deploy Jaeger Demo to OKE** run is queued or in progress. Rollback:
set it to `true`.

### 2. Capture evidence and suspend the preparer

Kubernetes events expire after about an hour, so read a failed preparation soon
after it happens.

```bash
kubectl -n jaeger-demo-operations get jobs,pods -o wide
```

```bash
kubectl -n jaeger-demo-operations get events --sort-by=.lastTimestamp
```

```bash
kubectl -n jaeger-demo-operations patch cronjob jaeger-demo-preparer --type=merge -p '{"spec":{"suspend":true}}'
```

Verify `kubectl -n jaeger-demo-operations get cronjob jaeger-demo-preparer -o jsonpath='{.spec.suspend}'`
prints `true` and no preparer Job is active. Rollback: patch `suspend` to `false`.

### 3. Read the request record and the lock

```bash
kubectl -n jaeger-demo-operations get configmap daily-hosted-update -o jsonpath='{.data.state} {.data.slot} {.metadata.uid} {.metadata.resourceVersion}{"\n"}'
```

```bash
kubectl -n default get configmap jaeger-demo-deployment-revisions -o jsonpath='{.metadata.annotations}{"\n"}{.metadata.resourceVersion}{"\n"}'
```

| Request state | Action |
| --- | --- |
| `succeeded`, `recovered`, `ready` or empty | Continue |
| `requested` | Wait until the record's `expiresAt` has passed, then read it again |
| `running` | Stop. An update is in progress or was interrupted. Treat it as an incident and inspect the executor before anything else |
| `failed` or `verified` | Reconcile it with the reviewed recovery command in [daily deployment requests](https://github.com/jaegertracing/jaeger-demo/blob/f9b54fe60b039633d00b5ea91dba6a40722e8fad/docs/daily-update-request.md#concurrency-and-recovery), with approval |

Record the value of `jaegertracing.io/deployment-lock-holder` if the second command
shows one.

### 4. Stop the executor

Only when the request state is not `requested` or `running`:

```bash
kubectl -n jaeger-demo-operations scale deployment jaeger-demo-executor --replicas=0
```

Verify no executor Pod remains. This frees a Pod slot on the node. Rollback:
scale to `1`.

### 5. Clear a stale lock holder

Only when step 3 showed a holder and you have confirmed its owner has stopped. A
`manual/<run>/<attempt>` holder is left by a failed manual run. A `daily/` holder
blocks the monitoring chart's hooks while the policies exist. Use the
`resourceVersion` from step 3 so a concurrent change is refused:

```bash
kubectl -n default annotate configmap jaeger-demo-deployment-revisions jaegertracing.io/deployment-lock-holder- --resource-version="$REVISIONS_RESOURCE_VERSION"
```

Verify the ConfigMap's UID and `data` are unchanged and the holder is gone. Leave
`jaegertracing.io/deployment-lock-version` in place until the end of this runbook.
Rollback: annotate it again with the recorded value.

### 6. Apply the deployer role and bind the manual identity

Follow [deployer access](deployer-access.md#apply-and-bind) at the reviewed commit:
a server-side dry run, the apply, the binding for the `demo-apply` identity only,
and `check --identity manual` reporting no difference. The role must not reuse a
name from the tables below; `jaeger-demo-deployer` and `jaeger-demo-recovery` do
not.

Rollback: delete the new bindings.

### 7. Upgrade each component manually

Before each run, confirm that every release it touches has Helm status `deployed`
(`helm status <release> -n <namespace>`). A manual run uninstalls and reinstalls a
`default/jaeger` release that is in any other state, which replaces its Service
addresses. It refuses a `default/prometheus` release that is neither `deployed`
nor `failed`, such as the `pending-upgrade` revision that
[#87](https://github.com/jaegertracing/jaeger-demo/issues/87) describes. Repair
such a release as an administrator first.

Dispatch **Deploy Jaeger Demo to OKE** from `main` once per component, in this
order, and verify each before the next:

1. `demo_stack=oci`, `otel_deploy_scope=monitoring`. This is the upgrade that
   failed in [#87](https://github.com/jaegertracing/jaeger-demo/issues/87).
2. `demo_stack=oci`, `otel_deploy_scope=jaeger`.
3. `demo_stack=otel`, `otel_deploy_scope=opensearch`. Choose the recovery policy
   deliberately; `required` creates and tests a backup first.
4. `demo_stack=otel`, `otel_deploy_scope=jaeger`.
5. `demo_stack=otel`, `otel_deploy_scope=otel-demo`.

Each run's first cluster step is the access check, so a run that lacks part of the
role stops before any change. Verify each component as in the
[change and verification process](releases.md#change-and-verification-process).
These runs also record all five fingerprints, so the first scheduled run selects
only the two Jaeger components unless something merged since.

Rollback: `helm rollback <release> <revision> -n <namespace>` to the revision in
the release record.

### 8. Bind the scheduled identity

Rerun the `bindings` command with both users and apply it, then run
`check --identity scheduled`. Do this only once steps 6 and 7 are accepted: from
here a credential with no reviewer holds the deployer role. Verify the GitHub
settings the repository cannot show:

- The `demo-scheduled` environment allows `main` only, with no tag rule.
- The OCI secrets exist only as environment secrets, not as repository secrets.
- Branch protection on `main` requires code-owner review of the latest push.
- The Renovate app's own settings do not enable automerge.

### 9. Enable the schedule

Run the read-only selection review in
[review scheduled scope](cutover.md#review-scheduled-scope), then:

```bash
gh variable set DEMO_SCHEDULE_ENABLED --repo jaegertracing/jaeger-demo --body true
```

Do not leave a manual run waiting for approval across 13:00 UTC: it would hold the
concurrency group and delay or cancel the scheduled run.

Do not re-run a scheduled run that was created before the direct path merged. A
re-run uses the workflow file of its own commit, so it would signal the request
record instead of deploying, and the re-run guard does not exist in that file.

### 10. Accept the first scheduled run

After the next 13:00 UTC run, record its URL, the configuration commit, the six
Helm revisions and the five fingerprints, and verify fresh traces on both demos,
the public routes and certificates, and the OpenSearch volume binding. The
executor objects stay in place until this run is accepted.

## Remove the executor objects

| # | Step | Approval |
| --- | --- | --- |
| 11 | Export everything to private custody | yes |
| 12 | Delete admission bindings, then policies | yes |
| 13 | Delete the executor's RBAC | yes |
| 14 | Delete the namespace | yes, cannot be undone |
| 15 | Delete the CRD | yes |
| 16 | Remove the lock annotation and retire leftovers | yes |

### 11. Export everything to private custody

Keep the exports outside Git; they include Secrets. They are the only rollback for
steps 12 and 13.

```bash
kubectl get validatingadmissionpolicies,validatingadmissionpolicybindings -o yaml > admission.yaml
```

```bash
kubectl get all,configmaps,secrets,serviceaccounts,networkpolicies,persistentvolumeclaims,roles,rolebindings,dailyworkloadapprovals -n jaeger-demo-operations -o yaml > operations.yaml
```

```bash
kubectl get rolebindings,clusterrolebindings -A -o json | jq -r '.items[] | select(any(.subjects[]?; .namespace == "jaeger-demo-operations")) | "\(.kind) \(.metadata.namespace // "-") \(.metadata.name) \(.roleRef.name)"' > executor-rbac.txt
```

Decide whether the outcome records on the `jaeger-demo-bundles` volume must be
kept. Copying them needs a Pod that mounts the volume, which is a separate
approved step.

### 12. Delete admission bindings, then policies

The executor's policies and bindings are named `jaeger-demo-*` or
`jaeger-reviewed-daily-request*`. Nothing else uses those prefixes for these two
kinds. List, review the list, then delete bindings before policies:

```bash
kubectl get validatingadmissionpolicybindings -o name | grep -E '/(jaeger-demo-|jaeger-reviewed-daily-request)' > bindings.txt
```

```bash
kubectl get validatingadmissionpolicies -o name | grep -E '/(jaeger-demo-|jaeger-reviewed-daily-request)' > policies.txt
```

```bash
xargs kubectl delete < bindings.txt
```

```bash
xargs kubectl delete < policies.txt
```

Verify both listings are now empty, wait ten seconds for the API server to drop
them, then confirm writes are admitted with a change that alters nothing:

```bash
kubectl -n default patch configmap jaeger-demo-deployment-revisions --type=merge -p '{}' --dry-run=server
```

Rollback: apply `admission.yaml` again while the parameters still exist.

### 13. Delete the executor's RBAC

Delete by exact name. The deployer roles share the `jaeger-demo-` prefix, so a
prefix match here would remove the deployment's own access. The executor's Roles
and RoleBindings are:

| Name | Scopes |
| --- | --- |
| `jaeger-demo-workload-observe` | ClusterRole and binding; Roles in `default`, `jaeger`, `opensearch`, `otel-demo`, `kube-system` |
| `jaeger-demo-existing-workload` | ClusterRole and binding; Roles in the same five namespaces |
| `jaeger-demo-release-ledger-read`, `jaeger-demo-workload-readiness`, `jaeger-demo-guarded-history` | `default`, `jaeger`, `opensearch`, `otel-demo` |
| `jaeger-demo-prior-snapshot-pods` | `default`, `jaeger` |
| `jaeger-demo-preview-only` | `default`, `jaeger`, `opensearch`, `otel-demo`, `kube-system` |
| `jaeger-demo-monitoring-hook` | ClusterRole and binding; Role in `default` |
| `jaeger-demo-guarded-existing` | Only if the optional workload grants were installed |

Compare that table with `executor-rbac.txt` from step 11, which lists what the
cluster really holds, and delete each Role, RoleBinding, ClusterRole and
ClusterRoleBinding it names. Objects inside `jaeger-demo-operations` go with the
namespace in the next step. Verify that `executor-rbac.txt`, generated again,
lists only objects in that namespace, and that `check` for both identities still
reports no difference.

### 14. Delete the namespace

This removes the request record, the approval objects, both key Secrets, the
bundle volume claim and the executor workloads. It cannot be undone. Read the
volume's reclaim policy first, so you know whether the block volume is deleted
with the claim:

```bash
kubectl get pv "$(kubectl -n jaeger-demo-operations get pvc jaeger-demo-bundles -o jsonpath='{.spec.volumeName}')" -o jsonpath='{.spec.persistentVolumeReclaimPolicy}{"\n"}'
```

```bash
kubectl delete namespace jaeger-demo-operations
```

Verify the namespace is gone, not `Terminating`, and that the volume is deleted or
`Released` as the reclaim policy says. If it stays in `Terminating`, a policy or
binding from step 12 remains; do not force-remove finalizers.

### 15. Delete the CRD

```bash
kubectl delete crd dailyworkloadapprovals.demo.jaegertracing.io
```

### 16. Remove the lock annotation and retire leftovers

No workflow reads the lock once the direct path has merged.

```bash
kubectl -n default annotate configmap jaeger-demo-deployment-revisions jaegertracing.io/deployment-lock-version-
```

Then retire, each with its own approval:

- The recovery operator's grant on the deployment-revisions ConfigMap.
- The scheduled identity's earlier read-only bindings and the manual identity's
  handover grants, which the deployer role replaces. Rerun `check` for both
  identities afterwards.
- The published `jaeger-demo-executor` container images, after deciding how long
  to keep them.
- The `demo-plan` GitHub environment, its OCI secrets and its identity's cluster
  bindings. No workflow uses that environment.

## Record

In the private release record: the exports and their hashes, the request record's
final state, the lock values removed, the volume's reclaim policy and the decision
on outcome records, the commit of the applied role with both `check` outputs, the
URL of each manual run in step 7 and of the first scheduled run with its six
revisions and five fingerprints, and the time of each deletion.

In [docs/MIGRATION.md](MIGRATION.md): the dates of the switch to the direct path,
the approvals, the accepted first run and the removal of the executor objects.
Production state does not go in the changelog, the README or a diagram.

Merge the change that removes the executor code and documents from the repository
only after step 16 is recorded.
