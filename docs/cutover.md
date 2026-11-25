<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Hosted-demo ownership handover

This runbook describes the Stage 1 script handover. It is not evidence that
production has migrated. [MIGRATION.md](MIGRATION.md) tracks the phase exits;
[releases.md](releases.md) defines the deployment record and verification contract.

The agent prepares and executes accepted work. Human verification is required for
the concrete access plan, first script handover, first Helmfile adoption and final
acceptance. Routine implementation, diagnostics and operations within accepted
scope proceed without repeated permission. Review-feedback replies require
individual human approval under the organization AI Usage Policy.

## Preserve the existing installation

| Namespace | Application releases |
| --- | --- |
| `default` | `jaeger`, `prometheus` |
| `jaeger` | `jaeger` |
| `opensearch` | `opensearch`, `opensearch-dashboards` |
| `otel-demo` | `otel-demo` |

Preserve the OKE cluster, public URLs/DNS, namespaces, PVCs, release names and
Service addresses. Certificate issuers remain externally managed. Do not combine
handover with infrastructure provisioning, ingress-controller/cert-manager
upgrades, DNS changes or AI activation.

## Prepare before disabling the old path

- Merge the tested extraction to main with its imported history intact. Verify
  CODEOWNERS, required review/checks and DCO/Renovate coverage. Production workflows
  must still be manual-only.
- Establish a fresh healthy baseline on both demos, including finite SPM calls and
  latency under current generated traffic. Existing defects are separate fixes and
  block handover. A Ready pod or HTTP 200 alone does not establish this baseline.
- Capture the complete affected release/resource state, chart/configuration/image
  inputs, prior revisions and rollback procedure. The preliminary inventory that
  omits embedded configuration is not full equivalence evidence.
- Freeze and hash the release record described in [releases.md](releases.md).
  Review every difference against live state. Preserve exact image reference syntax
  for the ownership transition; tag-to-digest conversion can roll pods even when
  the image bytes match.
- Verify the access/security plan, then provision fresh new-repository OCI access.
  Do not copy the old private key. Prepare the write-capable apply identity in
  `demo-apply`, restricted to main; the extraction workflows use that environment.
  Do not prepare a separate read-only plan identity or a `demo-plan` environment.
  Earlier versions of this runbook asked for both, for a generated plan workflow
  that the [migration plan](MIGRATION.md#execution-contract) retires, and no
  workflow uses them. Verify permissions without deployment writes before retiring
  access.

The current workflows refer to these secrets: `OCI_CLI_USER`, `OCI_CLI_TENANCY`,
`OCI_CLI_FINGERPRINT`, `OCI_CLI_KEY_CONTENT`, `OCI_CLI_REGION`,
`OKE_CLUSTER_OCID`, and the deployment failure notification's `SLACK_WEBHOOK_URL`.
Configure names and access scopes in the reviewed environment plan. Do not copy
credential values into documentation, issues, logs or inventories. The scheduled
apply environment must not require a human reviewer for each routine run after
its initial activation is accepted.

Useful read-only checks, from an authenticated administrative terminal:

```bash
gh api repos/jaegertracing/jaeger-demo/commits/main --jq .sha
helm list --all-namespaces -o json
helm history jaeger --namespace default -o json
helm history jaeger --namespace jaeger -o json
kubectl get pvc --all-namespaces
kubectl get ingress --all-namespaces
```

These commands are partial checks, not a complete inventory collector. Review any
additional captured output before sharing it. Helm 4.3.2 lists releases in every
status by default; `--all` is not supported. Keep status filters unset for the
inventory. The default limit is 256 releases, above the six application releases
listed here.

## Check the restricted handover path

Supplying `expected_config_sha` activates the Stage 1 handover guards. The run must
be a manual dispatch from main, select exactly one of `oci` or `otel`, use
`upgrade` and `jaeger`, and supply explicit full Jaeger and HotROD commit tags.
The reconciler checks its actual component selection and rejects any image-tag
change during resolution. Omitting the reviewed SHA uses the ordinary deployment
path; it is not an alternative for the first ownership transfer.

Before application writes, `scripts/demo/handover.py` requires both `default/jaeger`
and `jaeger/jaeger` to exist in `deployed` state using Helm Secret storage. The
implicit kubectl and Helm namespace must be `default`. It requires every target
chart object, generator, supplemental Service/Ingress and the shared
`default/jaeger-demo-deployment-revisions` ConfigMap to exist and be readable.
A denied read is a failure, not evidence that an object is absent. OCI refuses its
ordinary uninstall/reinstall fallback; OTel repeats the check before HotROD
replacement. Both handover Helm commands omit `--install`, and OTel also omits
`--create-namespace`. OTel ingress apply and certificate-read errors fail the run.

These guards do not compare full live manifests or authorize a new resource set.
Before dispatch, compare captured current, desired and rollback object sets, chart
hooks, exact image references and prior release revisions in the frozen record.
The only accepted application-object deletion/recreation is OTel's
`jaeger/jaeger-hotrod` Deployment. Missing objects, new hooks, unexplained revision
changes or failed/pending releases require a separate repair and fresh verification.
Drain old writers first and keep the new workflow serialized. Verify cluster
identity, expected certificates and public TLS independently; readable certificate
summaries alone do not prove TLS health.

Check effective access using the actual new CI principal before its first write:

| Capability | Initial handover requirement |
| --- | --- |
| Existing chart and supplemental objects | Named `get,patch`; chart/watcher workload reads include `list,watch` and dependent status resources |
| OTel HotROD replacement | Named Deployment `delete` plus Deployment `create` in `jaeger`; native RBAC cannot name-limit top-level create |
| Helm storage in `default` and `jaeger` | Secret `get,list,create`; `update,delete` restricted to individually listed retained Jaeger revisions and the next upgrade/rollback revisions |
| OTel diagnostics and TLS summaries | Service list in `kube-system`, relevant Namespace reads and Certificate list in `jaeger`, `opensearch`, `otel-demo` |
| Excluded mutations | Other application deletes/updates, OpenSearch Pod deletion, exec/port-forward, namespaces, storage, RBAC, CRDs/webhooks and AI ingress |

Secret list discloses namespace Secret contents, and top-level Secret create cannot
be name-limited with native RBAC. Keep that accepted trusted-writer scope explicit.
Use exact observed revision Secret names, not wildcard `resourceNames`; a retry
beyond the accepted revision range needs renewed verification. Include automatic
rollback and history pruning in the permission rehearsal. For example:

```bash
# Expected yes under the apply principal:
kubectl auth can-i patch deployments.apps/jaeger --namespace default
kubectl auth can-i delete deployments.apps/jaeger-hotrod --namespace jaeger
kubectl auth can-i create deployments.apps --namespace jaeger
kubectl auth can-i list services --namespace kube-system
# Expected no:
kubectl auth can-i delete deployments.apps/jaeger --namespace default
kubectl auth can-i delete pods --namespace opensearch
kubectl auth can-i create pods --subresource=exec --namespace opensearch
kubectl auth can-i create pods --subresource=portforward --namespace jaeger
```

These examples are not the complete permission matrix. Check all required verbs
and positive reads/watches against the verified object set; never test forbidden
writes by attempting them. RBAC grants are additive, so inspect other bindings as
well. Keep an independent administrator recovery route. The restricted writer is
not authorized to rebuild the installation or repair missing unrelated objects.

## Transfer workflow ownership

1. Verify the concrete handover record with the human. Confirm tools, chart
   dependencies and exact image tags still match the record. Supply the full
   reviewed configuration SHA to the workflow guard.
2. Disable all three core operational workflows:

   ```bash
   gh workflow disable ci-deploy-demo.yml --repo jaegertracing/jaeger
   gh workflow disable ci-opensearch-recovery.yml --repo jaegertracing/jaeger
   gh workflow disable ci-opensearch-volume-expand.yml --repo jaegertracing/jaeger
   ```

3. Drain both running and pending old runs. Disabling a workflow does not cancel
   an existing run. Inspect each workflow until no non-completed run remains:

   ```bash
   for workflow in ci-deploy-demo.yml ci-opensearch-recovery.yml ci-opensearch-volume-expand.yml; do
     gh api --paginate \
       "repos/jaegertracing/jaeger/actions/workflows/$workflow/runs?per_page=100" \
       --jq '.workflow_runs[] | select(.status != "completed") | {id,status,head_sha,html_url}'
   done
   ```

   Do not cancel an active Helm operation merely to expedite the handover. Inspect
   its state and use a reviewed completion/repair procedure. Matching concurrency
   group names do not coordinate across repositories.
4. After the old runs drain, retire their delivery of signing credentials. During
   this handover the operator removed core's repository Actions secret
   `OCI_CLI_KEY_CONTENT`. The corresponding maintainer OCI signing key remains
   active because other consumers could not be ruled out. Record the credential
   disposition and retain the tested independent recovery route.
5. Enable the new manual deployment path if GitHub has disabled it. Dispatch only
   the reviewed inputs from main. Transfer the OCI demo first, verify it, then
   transfer the OTel demo in a separate run. Use `otel_deploy_scope=jaeger` and
   each stack's existing Jaeger/HotROD commit tags. The workflow always runs
   `upgrade`. The stacks may run different image versions; one `demo_stack=all` invocation shares
   image inputs and cannot preserve those differences. Supply the full configuration SHA in
   `expected_config_sha`; the workflow fails before OKE setup if main has changed.
   Capture the shared fingerprint ConfigMap immediately before each dispatch, with
   a distinct snapshot for each stack. The pre-OTel capture must include the
   successful OCI result. Keep these with that dispatch's release and supplemental
   rollback captures; a snapshot from before both runs is insufficient for an
   OTel-only rollback.
   Set these variables from the verified record, then dispatch:

   ```bash
   : "${config_sha:?Set the reviewed full configuration SHA}"
   : "${oci_jaeger_tag:?Set the existing OCI Jaeger commit tag}"
   : "${oci_hotrod_tag:?Set the existing OCI HotROD commit tag}"
   gh workflow run ci-deploy-demo.yml --repo jaegertracing/jaeger-demo --ref main \
     -f "expected_config_sha=$config_sha" \
     -f demo_stack=oci -f otel_deploy_scope=jaeger \
     -f "jaeger_image_tag=$oci_jaeger_tag" -f "hotrod_image_tag=$oci_hotrod_tag"
   ```

   Wait for the OCI run to finish and verify its functional and resource checks.
   Only then dispatch the OTel run from the same reviewed configuration:

   ```bash
   : "${config_sha:?Set the reviewed full configuration SHA}"
   : "${otel_jaeger_tag:?Set the existing OTel Jaeger commit tag}"
   : "${otel_hotrod_tag:?Set the existing OTel HotROD commit tag}"
   gh workflow run ci-deploy-demo.yml --repo jaegertracing/jaeger-demo --ref main \
     -f "expected_config_sha=$config_sha" \
     -f demo_stack=otel -f otel_deploy_scope=jaeger \
     -f "jaeger_image_tag=$otel_jaeger_tag" -f "hotrod_image_tag=$otel_hotrod_tag"
   ```

   Verify and record each run's `headSha` and inputs. A differing configuration
   revision requires re-verification, not removal of the guard.

   These runs deploy the two Jaeger components. They do not exercise a chart
   upgrade of OpenSearch, monitoring or the OTel application. The full release and
   public-behavior checks still cover the existing six-release installation.
   The Jaeger-only scope does not invoke the OpenSearch upgrade path.
6. Verify actual changes and all health checks in [releases.md](releases.md). The
   extracted scripts retain unconditional generator restarts and OTel HotROD
   Deployment replacement; review those expected effects rather than claiming this
   script handover preserves all pod or Deployment identities. Unchanged identity
   and pod-template adoption tests belong to the later Helmfile transition.
7. Keep the daily script schedule disabled after the two manual runs. Their success
   verifies only the selected Jaeger components, not the schedule's selection or
   permissions. Obtain the separate scope/access decision described below before
   enabling it, then verify the first scheduled run. Stage 1 operational handover
   remains incomplete until that scheduled-refresh gate passes.

Generated trace history is disposable. The workflow has no recovery input, and
neither a manual nor a scheduled OpenSearch upgrade takes or requires a backup
([#76](https://github.com/jaegertracing/jaeger-demo/issues/76)). The existing PVC
and the live version and trace checks remain part of every upgrade
([OpenSearch storage maintenance](releases.md#opensearch-storage-maintenance)).

## Review scheduled scope

The reconciler fingerprints the whole deployment script for each component in its
stack. The handover guard changes therefore also change the OpenSearch, OTel Demo
and monitoring fingerprints. Two successful manual Jaeger runs update only their
own keys. A schedule selects every changed or missing component fingerprint plus
both Jaeger components; it does not use the manual stack/scope filter. The initial
apply role and its finite Helm revision window do not authorize this broader or
repeated work. The scheduled identity needs the deployer role defined in
[ops/rbac/deployer.yaml](../ops/rbac/deployer.yaml).

From the root of a clean checkout at the proposed merged configuration SHA,
inspect the actual selection with this read-only command. It uses the same fingerprint and
selection functions as the reconciler and reads only the shared ConfigMap:

```bash
: "${config_sha:?Set the proposed merged configuration SHA}"
python3 - "$config_sha" <<'PY'
import json
import subprocess
import sys

if subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != sys.argv[1]:
    raise SystemExit("Checkout does not match the proposed configuration SHA")
if subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], text=True).strip():
    raise SystemExit("Checkout has uncommitted changes")
sys.path.insert(0, "scripts/demo")
import deploy

state = json.loads(subprocess.check_output([
    "kubectl", "-n", "default", "get", "configmap", deploy.STATE, "-o", "json",
], text=True))
desired = {name: deploy.fingerprint(name) for name in deploy.COMPONENTS}
selected = deploy.selected_components("schedule", "oci", "jaeger", "upgrade", state.get("data", {}), desired)
print(json.dumps({"configuration": sys.argv[1], "state_resource_version": state["metadata"]["resourceVersion"],
                  "selected_components": selected}, indent=2))
PY
```

Retain the result and review every selected component, its
effective permissions, revision allowance and rollback procedure. Rehearse the
accepted selection with matching permissions before activation, and repeat this
inspection if configuration or stored fingerprints change. Do not overwrite
unapplied fingerprints to hide additional selections. The initial manual access
approval is not approval to broaden permissions or remove the environment reviewer.

The maintainers selected all five components for the 13:00 UTC job. The
scheduled run is the same `deploy` job of `ci-deploy-demo.yml` as a manual run,
in the `demo-scheduled` environment, and runs `scripts/demo/deploy.py` directly.
Keep the repository Actions variable `DEMO_SCHEDULE_ENABLED` set to `false` until
the steps below are complete. Confirm the effective value if the organization
defines the same name. A skipped scheduled job receives no deployment environment
or runner. Manual runs continue to use `demo-apply`.

1. Review the pinned configuration and all selected releases. For OpenSearch,
   capture the existing StatefulSet, PVC binding, release revision, running
   image and version. Verify the paired Dashboards version and a fresh trace
   before the change. The scheduled upgrade preserves the live volume and
   repeats the version and trace checks afterward. Existing synthetic trace
   history can be lost. Any destructive reset needs its own approval.
2. Review the [deployer access](deployer-access.md) and the
   [chart inventory](../ops/rbac/chart-objects.json). Verify that the main-only
   `demo-scheduled` environment has no reviewer and that an administrator has
   applied `ops/rbac/deployer.yaml` and bound the scheduled identity. Run
   `deployer_access.py check --identity scheduled --user <scheduled user OCID>`
   and require `0 difference(s)`. The scheduled identity does not hold the
   `jaeger-demo-recovery` role, which only volume expansion uses.
   Keep the human reviewer on `demo-apply`. Follow
   [Before the first direct run](executor-retirement.md#before-the-first-direct-run)
   in the executor retirement procedure up to enabling the schedule, which is
   step 3 here.
3. With explicit activation approval, set the repository variable to `true`
   before the intended 13:00 UTC event. Do not dispatch a manual writer close to
   13:00 UTC, and do not leave a dispatched run waiting for approval across it:
   the writers share one concurrency group, and a newer waiting run cancels an
   older one ([what to do when that happens](releases.md#a-waiting-run-replaced-by-a-newer-one)).
   To pause future scheduled jobs, set the variable to `false`. Inspect
   and drain any queued or running writers before changing access or rolling back.

   ```bash
   gh variable set DEMO_SCHEDULE_ENABLED --body true --repo jaegertracing/jaeger-demo
   ```

   To pause:

   ```bash
   gh variable set DEMO_SCHEDULE_ENABLED --body false --repo jaegertracing/jaeger-demo
   ```

4. Find the scheduled run, check its event, configuration SHA, environment,
   access check and component summary, and compare all six live release
   histories and fingerprint keys with the pre-run record. A skipped job does
   not satisfy the stage exit.
   Verify the [per-component checks](releases.md#daily-component-updates) and
   the public health checks on both demos. Record the run URL, versions, trace
   ID, and PVC binding before closing [core handover](https://github.com/jaegertracing/jaeger/issues/10189).

   ```bash
   gh run list --repo jaegertracing/jaeger-demo --workflow ci-deploy-demo.yml \
     --event schedule --limit 5 --json databaseId,headSha,status,conclusion,url
   ```

The credential-free [public smoke check](../scripts/demo/README.md#scheduled-smoke-check)
has its own switch, `DEMO_SMOKE_ENABLED`. `SLACK_WEBHOOK_URL` is already a
repository secret. The check retries each stack up to three times and posts to
Slack only when the scheduled result changes, one message when it starts
failing and one when it recovers. Enable the switch after one passing manual
dispatch of `demo-smoke.yml` that shows no history lookup warning.

While the old maintenance workflows are disabled and the new grant is limited to
Jaeger, the independent operator owns maintenance and incident recovery through the
separate administrator route. Volume expansion uses a
`demo-apply` workflow and the storage access of the `jaeger-demo-recovery` role.
No workflow takes or restores a backup, so no identity needs snapshot privileges
or restore capacity.

## If the handover fails

Stop new automation first and retain diagnostics. Do not reactivate old workflows
while the new path can still write. Restore application revisions and supplemental
specs using the recorded scope, then verify health. See
[release failure handling](releases.md#failure-handling).

Restore the failed dispatch's application and supplemental resources first. After
verification, restore only its key in `default/jaeger-demo-deployment-revisions`
from that dispatch's pre-run capture: `oci-jaeger` or `otel-jaeger`. Use a merge
patch containing only that entry under `data`; if the key was originally absent,
use a null value to remove it. Read the current ConfigMap before patching and
verify afterward that every other entry is unchanged. Never apply an entire old
ConfigMap snapshot. In particular, an OTel-only rollback must preserve the earlier
successful OCI fingerprint. An unchanged failed-component key needs no patch.

Keep the retired Actions secret absent from core. If returning operational ownership to core is necessary,
verify a replacement scoped credential and the exact old configuration before
enabling that path. Give exactly one repository write access to production at a time.
Do not remove the legacy fingerprint ConfigMap during handover.

## Helmfile adoption

This is the operator runbook for phase 6 of the [migration plan](MIGRATION.md):
manual adoption of the hosted demo by the deploy workflow's
[Helmfile engine](releases.md#helmfile-engine), under the direct contract (one
reviewed commit on `main`, applied by **Deploy Jaeger Demo to OKE**, no bundle and
no second identity). The engine does every write to a release. This runbook is
the operator's part: the checks before, the ownership patches, the dispatches and
what to check after each, recovery and rollback. A maintainer walks it once before
production adoption ([#74](https://github.com/jaegertracing/jaeger-demo/issues/74)).
Production runs the scripts until then.

The invariants hold throughout:

- Adopt only the allowlisted supplemental objects
  ([charts/demo-resources/allowlist.yaml](../charts/demo-resources/allowlist.yaml))
  into the four namespace-local `demo-resources` releases, with the keep policy.
  Reject foreign ownership. Keep every UID, selector, Service ClusterIP, pod
  template and PVC binding. Never template PVCs, namespaces, snapshots or
  generated TLS Secrets. A first companion install is a plain `helm install`,
  not atomic.
- [helmfile/images.yaml](../helmfile/images.yaml) stays `mode: live` for the whole
  adoption: the engine renders exactly the image references that run, and a
  live-mode apply restarts nothing and is refused before any write if a read-only
  diff of any release is not empty. Switching to `digest` is a later, separate
  reviewed pull request ([phase 7](MIGRATION.md#7-promotion-and-acceptance)).
- Every apply reconciles all ten releases, OCI before OTel. A first companion
  install is a manual dispatch for one namespace and never runs on a schedule.
- Companion rollback never uses `helm uninstall`, which would delete the adopted
  objects: use [De-adopt companion releases](#de-adopt-companion-releases). An
  OpenSearch or Dashboards downgrade is not a rollback. The scripts can be a
  fallback only after de-adoption.
- From the first companion install until the schedule switches to Helmfile, the
  schedule stays paused and no run uses `engine=scripts`: the scripts
  `kubectl apply` the supplemental objects that the companions then own
  ([the engine switch](releases.md#the-engine-switch)). Plan the `mode: digest`
  pull request to follow adoption closely, because there is no daily refresh in
  between.

### How the commands run and how they are rehearsed

Each step runs in one Cloud Shell session, from a clean checkout of the reviewed
commit, in a shell that has the pinned Helm and Helmfile first on its `PATH`.
`kubectl` and Helm read `$KUBECONFIG`, which is a private copy of the cluster
administrator's kubeconfig whose every request impersonates the manual
(`demo-apply`) deployer identity
([scripts/demo/deployer_kubeconfig.py](../scripts/demo/deployer_kubeconfig.py)).
Each step therefore has exactly the access of
[ops/rbac/deployer.yaml](../ops/rbac/deployer.yaml), and the ownership planner
needs no credentials of its own. A step that needs more says so and passes the
administrator kubeconfig itself. These are the only ones, and each needs
something the deployer role deliberately does not grant:

| Administrator step | Why the deployer role cannot run it |
| --- | --- |
| `deployer_access.py check --user`, both identities | It creates SubjectAccessReviews, which the role does not grant |
| Read Certificates in `default` | The role lists Certificates only in the three OTel namespaces |
| Read events and Pod logs in `default` | The role reads Pods and ReplicaSets there, not events or logs |
| Delete an orphaned OpenSearch Pod | The role creates and deletes no Pod |

The variables below are the only input to the commands. Set them as the steps say.

| Variable | Set in | Meaning |
| --- | --- | --- |
| `REVIEWED_COMMIT` | [Operator shell](#operator-shell) | The full SHA of `main` that the maintainer reviewed for adoption |
| `ADMIN_KUBECONFIG`, `ADMIN_CONTEXT` | Operator shell | The administrator's Cloud Shell kubeconfig and its demo-cluster context, which is its current context |
| `MANUAL_USER_OCID`, `SCHEDULED_USER_OCID` | Operator shell | The OCI user OCIDs of the `demo-apply` and `demo-scheduled` identities. The repository never records them |
| `RECORD` | Operator shell | A private directory outside the checkout, kept with the release record |
| `KUBECONFIG`, `CONTEXT` | Operator shell | The deployer kubeconfig and its only context |
| `ns` | Each namespace step | A release namespace: `default`, `jaeger`, `opensearch` or `otel-demo` |
| `release`, `REVISION` | Recovery and rollback | A release name in `$ns`, and the revision to return to |
| `RUN_ID` | After each dispatch | The run of **Deploy Jaeger Demo to OKE** whose record a step reads |
| `STEP`, `FROM` | Identity captures | The name of a capture, and the earlier capture to compare it with (`baseline` unless a step says otherwise) |

The `operations` CI job rehearses this runbook on kind with
[scripts/demo/rehearsals/helmfile-runbook/exercise.py](../scripts/demo/rehearsals/helmfile-runbook/exercise.py).
At run time it reads every block below marked `<!-- rehearse: <id> -->` (visible
in the Markdown source) from this file and runs it verbatim with
`bash -euo pipefail`, from the repository root, with only these variables set:
as the fixture's manual deployer user, through a kubeconfig written by the
operator shell step, and the administrator steps with the kind administrator
kubeconfig. The fixture is the one of the Helmfile engine rehearsal: synthetic
application charts under the real release names, the real `demo-resources` chart
and sources, and an OpenSearch StatefulSet with a bound claim. It walks the
ownership patches and first install of each namespace, the adoption apply and
the no-op, a failed release and its recovery, a pending release refused and then
recovered, a rollback to a recorded revision and the next apply that undoes it,
the StatefulSet repair, and de-adoption, and after each step it checks what the
step here promises. Blocks marked `not-rehearsed` need GitHub, OCI, the public
demo or the real charts, and the marker says what the rehearsal does instead: a
workflow dispatch becomes a call of `helmfile_deploy.reconcile` with the
environment the dispatch gives the **Reconcile demo components** step (stub image
lookup, health and trace checks), and the artifact download becomes a copy of
that call's `deploy-record.json` to `$RECORD/runs/$RUN_ID`.
[scripts/demo/runbook_blocks_test.py](../scripts/demo/runbook_blocks_test.py),
part of `make test`, fails when a bash block of this section has no marker, when
a rehearse id is duplicated or missing from the rehearsal's step list, and when a
block names the retired plan or apply identity, deletes Secrets by selector or
uninstalls a release. OKE authentication, Cloud Shell and the production
bindings are not rehearsed.

### Operator shell

The cluster administrator opens Cloud Shell in the tenancy of the demo cluster and
creates its kubeconfig as the console's **Access Cluster** dialog shows
(`oci ce cluster create-kubeconfig` with the public endpoint), so that its current
context is the demo cluster. The administrator must be allowed to impersonate
users, as a cluster administrator is. Then, with the reviewed commit at hand:

<!-- not-rehearsed: Cloud Shell, the clone and the prompts for the two OCIDs; the rehearsal runs from the CI checkout with the kind administrator kubeconfig and fixture users -->
```bash
git clone https://github.com/jaegertracing/jaeger-demo.git && cd jaeger-demo
read -rp "Reviewed commit: " REVIEWED_COMMIT && git checkout --detach "$REVIEWED_COMMIT"
git status --porcelain --untracked-files=all   # must print nothing
read -rp "demo-apply OCI user OCID: " MANUAL_USER_OCID
read -rp "demo-scheduled OCI user OCID: " SCHEDULED_USER_OCID
export REVIEWED_COMMIT MANUAL_USER_OCID SCHEDULED_USER_OCID
ADMIN_KUBECONFIG="$HOME/.kube/config"
ADMIN_CONTEXT=$(kubectl --kubeconfig "$ADMIN_KUBECONFIG" config current-context)
RECORD="$HOME/helmfile-adoption-$(date -u +%Y%m%dT%H%M%SZ)"
export ADMIN_KUBECONFIG ADMIN_CONTEXT RECORD
python3 scripts/demo/helmfile_tools.py run -- bash
```

The last command starts the pinned shell; every later step runs in it. Write the
deployer kubeconfig, select it, and confirm whom the requests run as. The
kubeconfig is created with mode 0600 and never overwritten:

<!-- rehearse: operator-kubeconfig -->
```bash
umask 077
mkdir -p "$RECORD/runs"
python3 scripts/demo/deployer_kubeconfig.py --kubeconfig "$ADMIN_KUBECONFIG" --context "$ADMIN_CONTEXT" \
  --user "$MANUAL_USER_OCID" --output "$RECORD/deployer.kubeconfig"
export KUBECONFIG="$RECORD/deployer.kubeconfig" CONTEXT="$ADMIN_CONTEXT"
helm version --short
if [[ "$(kubectl auth whoami -o jsonpath='{.status.userInfo.username}')" == "$MANUAL_USER_OCID" ]]; then
  echo "kubectl and Helm act as the manual deployer identity"
else
  echo "Stop: the deployer kubeconfig does not impersonate the manual identity" >&2
  false
fi
```

`helm version` must print v4.3.2. Check that the impersonated identity holds the
whole committed role:

<!-- rehearse: impersonated-access -->
```bash
python3 scripts/demo/deployer_access.py check --identity manual --self
```

It must report `manual: 0 difference(s)`. If the shell ends, start the pinned
shell again from the checkout and export `RECORD`, `KUBECONFIG` and `CONTEXT`
again; the files are kept.

### Read-only checks before adoption

None of these writes to the cluster. Keep every output in `$RECORD`.

1. The schedule is paused, Helmfile is not the scheduled engine, and no writer is
   queued or running. Run this wherever the maintainer's GitHub CLI is signed in:

   <!-- not-rehearsed: GitHub repository variables and workflow runs -->
   ```bash
   gh variable list --repo jaegertracing/jaeger-demo
   for workflow in ci-deploy-demo.yml ci-opensearch-reset.yml ci-opensearch-volume-expand.yml; do
     gh api --paginate \
       "repos/jaegertracing/jaeger-demo/actions/workflows/$workflow/runs?per_page=100" \
       --jq '.workflow_runs[] | select(.status != "completed") | {id, event, status, created_at, html_url}'
   done
   ```

   `DEMO_SCHEDULE_ENABLED` must be `false`, `DEMO_DEPLOY_ENGINE` must be absent and
   the loop must print nothing. To pause the schedule:

   <!-- not-rehearsed: GitHub repository variable -->
   ```bash
   gh variable set DEMO_SCHEDULE_ENABLED --body false --repo jaegertracing/jaeger-demo
   ```

2. The six application releases, each `deployed`, and the history of each, which
   holds the revisions a rollback before adoption would return to:

   <!-- rehearse: releases-before -->
   ```bash
   for namespace in default jaeger opensearch otel-demo; do
     helm list --namespace "$namespace" --max 0 -o json
   done | jq -s 'add' > "$RECORD/releases-before.json"
   jq -r '.[] | [.namespace, .name, .revision, .status, .chart, .app_version] | @tsv' "$RECORD/releases-before.json"
   jq -r '.[] | "\(.namespace) \(.name)"' "$RECORD/releases-before.json" |
     while read -r namespace name; do
       helm history "$name" --namespace "$namespace" --max 10 -o json > "$RECORD/history-before-$namespace-$name.json"
     done
   jq -e 'length == 6 and all(.[]; .status == "deployed")' "$RECORD/releases-before.json"
   ```

3. The access of both identities against the committed role. This is an
   administrator step:

   <!-- rehearse: access-check -->
   ```bash
   KUBECONFIG="$ADMIN_KUBECONFIG" python3 scripts/demo/deployer_access.py check --identity manual --user "$MANUAL_USER_OCID"
   KUBECONFIG="$ADMIN_KUBECONFIG" python3 scripts/demo/deployer_access.py check --identity scheduled --user "$SCHEDULED_USER_OCID"
   ```

   Each must report `0 difference(s)` ([deployer access](deployer-access.md#verify-the-live-access)).

4. The [live drift report](helmfile-applications.md#live-drift-report), run as the
   deployer:

   <!-- not-rehearsed: renders the pinned public charts and reads the production objects; the kind fixture holds synthetic application releases -->
   ```bash
   python3 scripts/demo/live_drift.py --kubeconfig "$KUBECONFIG" --context "$CONTEXT" \
     --output "$RECORD/live-drift-before.json"
   jq '.summary' "$RECORD/live-drift-before.json"
   ```

   It must exit 0: nothing `unexplained` and nothing `missing`. Each companion
   object is `explained`, with `companion-unadopted` among its reasons. Review every
   reason code against the table in that document.

5. The Certificates of the OTel routes, each `Ready`, and, as the administrator,
   those in `default`:

   <!-- rehearse: certificates -->
   ```bash
   for namespace in jaeger opensearch otel-demo; do
     kubectl --namespace "$namespace" get certificates.cert-manager.io -o json |
       jq -r --arg ns "$namespace" '.items[] | [$ns, .metadata.name,
         ([.status.conditions[]? | select(.type == "Ready") | .status][0] // "Unknown")] | @tsv'
   done | tee "$RECORD/certificates-before.tsv" | awk -F '\t' '$3 != "True" {bad = 1} END {exit bad || NR == 0}'
   ```

   <!-- rehearse: certificates-default -->
   ```bash
   kubectl --kubeconfig "$ADMIN_KUBECONFIG" --context "$ADMIN_CONTEXT" --namespace default \
     get certificates.cert-manager.io
   ```

6. Health of both stacks, and the functional baseline of
   [step 5 of the change process](releases.md#change-and-verification-process):
   fresh traces in both Jaeger UIs, finite SPM, Grafana, the shop and TLS.

   <!-- not-rehearsed: reaches the public demo endpoints -->
   ```bash
   python3 scripts/demo/stack_health.py --stack oci
   python3 scripts/demo/stack_health.py --stack otel
   ```

7. The identities that adoption must keep: the UID, selector and pod template of
   every Deployment, DaemonSet and StatefulSet in the four namespaces, the UID,
   ClusterIPs and selector of every Service, the pod-template hash of each active
   ReplicaSet, and the UID, volume and phase of the OpenSearch data claim. Set
   `STEP=baseline` for this first capture. The file holds pod templates, so it
   stays in the private record:

   <!-- rehearse: capture-identities -->
   ```bash
   : "${STEP:?Name this capture, for example baseline}"
   claim=$(kubectl --namespace opensearch get pod opensearch-cluster-single-0 -o json | jq -er '
     [.spec.containers[] | select(.name == "opensearch") | .volumeMounts[]
      | select(.mountPath == "/usr/share/opensearch/data") | .name] as $mounted
     | .spec.volumes[] | select(.name | IN($mounted[])) | .persistentVolumeClaim.claimName')
   {
     for namespace in default jaeger opensearch otel-demo; do
       case "$namespace" in
         jaeger) kinds=deployments.apps,services ;;
         opensearch) kinds=deployments.apps,statefulsets.apps,services ;;
         *) kinds=deployments.apps,daemonsets.apps,services ;;
       esac
       kubectl --namespace "$namespace" get "$kinds,replicasets.apps" -o json
     done
     kubectl --namespace opensearch get persistentvolumeclaim "$claim" -o json
   } | jq -s '[.[] | (.items // [.])[]
       | {kind, namespace: .metadata.namespace, name: .metadata.name, uid: .metadata.uid}
         + (if .kind == "Service" then {clusterIPs: .spec.clusterIPs, selector: .spec.selector}
            elif .kind == "ReplicaSet" then {podTemplateHash: .metadata.labels["pod-template-hash"], replicas: .spec.replicas}
            elif .kind == "PersistentVolumeClaim" then {volume: .spec.volumeName, phase: .status.phase}
            else {selector: .spec.selector, template: .spec.template} end)
       | select(.kind != "ReplicaSet" or .replicas > 0)] | sort_by(.kind, .namespace, .name)' \
     > "$RECORD/identities-$STEP.json"
   jq -e 'any(.[]; .kind == "PersistentVolumeClaim" and .phase == "Bound")' "$RECORD/identities-$STEP.json"
   ```

   Later steps take a capture with another `STEP` and compare it:

   <!-- rehearse: compare-identities -->
   ```bash
   : "${FROM:=baseline}" "${STEP:?Name the capture to compare}"
   diff -u "$RECORD/identities-$FROM.json" "$RECORD/identities-$STEP.json" && echo "Nothing changed since $FROM"
   ```

8. A read-only diff of the ten releases in live mode, which must show no
   application change. `prepare` reaches only the public chart sources:

   <!-- not-rehearsed: renders the pinned public charts against the production releases; the rehearsal runs helmfile_deploy.diff on its fixture state -->
   ```bash
   python3 scripts/demo/helmfile_deploy.py prepare
   python3 scripts/demo/helmfile_deploy.py diff | tee "$RECORD/diff-before.txt"
   ```

   The six application releases must print `no changes`. The four
   `<namespace>/demo-resources` lines print `error`, because a diff refuses a
   release that is not installed, so the command exits 1 at this point.

Then review, with the maintainer, the adoption render at `$REVIEWED_COMMIT`, the
allowlist, the live image references the diff read, the prior revisions in
`releases-before.json`, and the drift report. Record the decision and its time in
the release record.

### Apply the ownership patches

Adopt one namespace at a time, in this order: `default`, then `otel-demo`,
`opensearch` and `jaeger`. The OCI stack goes first so that its companion is
installed and verified before any OTel change, as the migration plan requires;
the OTel namespaces follow from the one that holds only an Ingress to the one
with the trace generator. For each namespace, set `ns`, apply its patches here and
install its companion with the [next step](#install-each-companion-release)
before moving on.

The planner reads the allowlisted objects of `$ns` by name as the deployer. Each
patch adds only the Helm ownership label, the two release annotations and the
keep policy, and tests the UID and resource version it was planned for:

<!-- rehearse: ownership-plan -->
```bash
python3 scripts/demo/companion_ownership.py --kubeconfig "$KUBECONFIG" --context "$CONTEXT" \
  --namespace "$ns" > "$RECORD/ownership-$ns.json"
jq -r '.[] | "\(.kind)/\(.name): \(.patch | length) operation(s)"' "$RECORD/ownership-$ns.json"
```

Review the plan. Then apply it, which needs only `patch` on the named objects, and
plan again; the second plan must hold only empty patches:

<!-- rehearse: ownership-apply -->
```bash
bash scripts/demo/apply-ownership-patches.sh "$RECORD/ownership-$ns.json" \
  "$KUBECONFIG" "$CONTEXT" "$RECORD/adopted.json"
python3 scripts/demo/companion_ownership.py --kubeconfig "$KUBECONFIG" --context "$CONTEXT" \
  --namespace "$ns" | jq -e 'all(.[]; .patch == [])'
```

The script appends each patch that succeeds to `adopted.json`. That file records
exactly what was added to every object and is the only input that de-adoption
accepts. Never edit it, replace it or write it by hand.

The script refuses a plan that contains anything other than guarded label and
annotation changes on the four allowlisted kinds, and stops at the first failed
patch. A patch fails without a write when its object changed after planning. To
retry, plan again into a new file, review it and apply it with the same
`adopted.json`. A fresh plan has an empty patch for each object that is already
adopted, so its entry in the record stays as first written. The script refuses a
plan that would patch an object the record already holds. If the script is
interrupted, compare the planner output with the record before continuing: an
object with an empty patch that the record does not list, and that was not owned
before the first plan, was patched without being recorded.

### Install each companion release

The first install of `$ns/demo-resources` is a manual dispatch of the deploy
workflow for that namespace. `main` must still be at `$REVIEWED_COMMIT`, or the
workflow's revision check stops the run before it starts. A `demo-apply` reviewer
approves it:

<!-- not-rehearsed: a workflow dispatch with a demo-apply reviewer; the rehearsal calls helmfile_deploy.reconcile with HELMFILE_OPERATION=install-companion and COMPANION_NAMESPACE=$ns -->
```bash
gh workflow run ci-deploy-demo.yml --repo jaegertracing/jaeger-demo --ref main \
  -f engine=helmfile -f expected_config_sha="$REVIEWED_COMMIT" \
  -f helmfile_operation=install-companion -f companion_namespace="$ns"
```

The engine refuses the install, with no write, unless every allowlisted object of
`$ns` exists with the ownership the patches added, no release in the four
namespaces is pending or in an unknown state, and the live drift classification
of the companion's objects finds nothing but ownership. Then it runs a plain
`helm install` without `--take-ownership` and restarts nothing
([what a companion install checks](releases.md#helmfile-engine)). Find the run,
wait for it, and copy its record into the private record. A re-run has another
attempt number in the artifact name:

<!-- not-rehearsed: GitHub runs and artifacts; the rehearsal copies the deploy-record.json of its reconcile call to $RECORD/runs/$RUN_ID -->
```bash
gh run list --repo jaegertracing/jaeger-demo --workflow ci-deploy-demo.yml --event workflow_dispatch \
  --limit 3 --json databaseId,headSha,status,conclusion,url
read -rp "Run ID: " RUN_ID && export RUN_ID
gh run watch "$RUN_ID" --repo jaegertracing/jaeger-demo --exit-status
gh run download "$RUN_ID" --repo jaegertracing/jaeger-demo --name "deploy-record-$RUN_ID-1" \
  --dir "$RECORD/runs/$RUN_ID"
```

The record must show one plain install of an absent release to deployed
revision 1, no generator restart and no fingerprint, both stacks verified where
the namespace is an OTel one, and the planner must find nothing left to patch:

<!-- rehearse: install-check -->
```bash
jq -e --arg ns "$ns" '.outcome == "succeeded" and .operation == "install-companion"
  and .companionNamespace == $ns and .generatorRestarts == [] and .fingerprints == []
  and .verifiedStacks == (if $ns == "default" then ["oci"] else ["oci", "otel"] end)
  and ([.releases[] | [.id, .statusBefore, .method, .revisionAfter, .statusAfter]]
       == [[$ns + "/demo-resources", "absent", "install", 1, "deployed"]])' \
  "$RECORD/runs/$RUN_ID/deploy-record.json"
helm history demo-resources --namespace "$ns" --max 1 -o json |
  jq -e '(.[0].revision | tostring) == "1" and .[0].status == "deployed"'
python3 scripts/demo/companion_ownership.py --kubeconfig "$KUBECONFIG" --context "$CONTEXT" \
  --namespace "$ns" | jq -e 'all(.[]; .patch == [])'
```

Then capture the identities with `STEP=companion-$ns` and compare them with the
baseline: nothing may differ. Check the health of the stack. Only then move to the
next namespace.

If the install fails, it leaves revision 1 `failed` and changes no object's
identity. Recover with step 1 of
[De-adopt companion releases](#de-adopt-companion-releases), keep the ownership
metadata, and dispatch the install again after the cause is fixed.

### Adopt and verify the no-op

With all four companions installed, dispatch an apply of the ten releases in
live mode, and find and copy its record as for an install:

<!-- not-rehearsed: a workflow dispatch with a demo-apply reviewer; the rehearsal calls helmfile_deploy.reconcile with HELMFILE_OPERATION=apply in live mode -->
```bash
gh workflow run ci-deploy-demo.yml --repo jaegertracing/jaeger-demo --ref main \
  -f engine=helmfile -f expected_config_sha="$REVIEWED_COMMIT"
```

The engine reads the live Jaeger and HotROD references, runs a read-only diff of
every release with the apply's own values and refuses the run if any would
change, then applies each release, which changes nothing, verifies OCI and then
OTel, and patches the five component fingerprints. The record must show exactly
that:

<!-- rehearse: adoption-record -->
```bash
jq -e '.outcome == "succeeded" and .operation == "apply" and .imageMode == "live"
  and .compatibility == "live: not revalidated" and .verifiedStacks == ["oci", "otel"]
  and .generatorRestarts == [] and (.releases | length) == 10 and (.fingerprints | length) == 5
  and all(.releases[]; .statusBefore == "deployed" and .method == "apply" and .changed == false
          and .revisionAfter == .revisionBefore)' "$RECORD/runs/$RUN_ID/deploy-record.json"
jq '.images' "$RECORD/runs/$RUN_ID/deploy-record.json"
```

The image references must be the ones running before adoption. Capture the
identities with `STEP=adoption` and compare them with the baseline: nothing may
differ. Then dispatch the same apply a second time and repeat these checks with
`STEP=no-op`. The ten releases must all be `deployed`:

<!-- rehearse: releases-after -->
```bash
for namespace in default jaeger opensearch otel-demo; do
  helm list --namespace "$namespace" --max 0 -o json
done | jq -s 'add' > "$RECORD/releases-after.json"
jq -r '.[] | [.namespace, .name, .revision, .status] | @tsv' "$RECORD/releases-after.json"
jq -e 'length == 10 and all(.[]; .status == "deployed")' "$RECORD/releases-after.json"
```

Run the drift report again and compare the Service allocations, which hash each
Service's UID and addresses, with the report from before:

<!-- not-rehearsed: the drift report needs the real charts and objects, as in the checks before adoption -->
```bash
python3 scripts/demo/live_drift.py --kubeconfig "$KUBECONFIG" --context "$CONTEXT" \
  --output "$RECORD/live-drift-after.json"
diff <(jq -S '[.objects[] | select(.serviceAllocationSHA256) | {identity, serviceAllocationSHA256}]' \
         "$RECORD/live-drift-before.json") \
     <(jq -S '[.objects[] | select(.serviceAllocationSHA256) | {identity, serviceAllocationSHA256}]' \
         "$RECORD/live-drift-after.json")
```

It must exit 0 with no difference in the allocations, and no companion object may
still be `companion-unadopted`. Repeat the health and functional checks. Record
the two run URLs, their records, the captures and the reports in the release
record. The human verification of this result is the phase 6 exit; record it in
[MIGRATION.md](MIGRATION.md). Keep the schedule paused until the `mode: digest`
change and the engine switch of phase 7.

### Recover a failed release

A release is `failed` when its last Helm transaction failed: the record's `error`
names it and its `statusAfter` is `failed`. Diagnose before any retry. Set `ns`
and `release`, then read its history, the reason Helm stored and its Pods:

<!-- rehearse: release-diagnostics -->
```bash
helm history "$release" --namespace "$ns" --max 5
helm status "$release" --namespace "$ns" -o json | jq -r '.info.status + ": " + .info.description'
kubectl --namespace "$ns" get pods -o wide
```

<!-- rehearse: release-events -->
```bash
if [[ "$ns" == default ]]; then
  # The deployer role reads no events in default: this read is the administrator's.
  kubectl --kubeconfig "$ADMIN_KUBECONFIG" --context "$ADMIN_CONTEXT" --namespace "$ns" \
    get events --sort-by=.lastTimestamp
else
  kubectl --namespace "$ns" get events --sort-by=.lastTimestamp
fi
```

Read a failing Pod's log with `kubectl --namespace "$ns" logs <pod>`; in `default`
that is an administrator read as well. Then fix the cause. A configuration cause
is a reviewed commit on `main`, never a change made by hand.

- **Image mode `digest`** (after phase 7 switches it): dispatch an apply as in
  [Adopt and verify the no-op](#adopt-and-verify-the-no-op). The engine syncs a
  `failed` release even when its diff is empty.
- **Image mode `live`** (during adoption): the engine refuses every apply while a
  release is not `deployed`, because Helmfile reports such a release as changed
  and a live-mode apply must change nothing. Return the release to its last
  deployed revision instead. This prints that revision, and stops unless the
  latest revision is `failed` or pending and the one before it is `deployed`:

  <!-- rehearse: last-deployed -->
  ```bash
  helm history "$release" --namespace "$ns" --max 2 -o json | jq -er '
    if length == 2 and .[0].status == "deployed"
       and (.[1].status == "failed" or (.[1].status | startswith("pending-")))
    then .[0].revision
    else error("Stop: the latest revision is not failed or pending, or the one before it is not deployed") end'
  ```

  Set `REVISION` to the printed number, roll back with
  [Roll back to a recorded revision](#roll-back-to-a-recorded-revision), and then
  dispatch the live-mode apply again. Its record must show the no-op of the
  adoption step.

A failed first companion install is recovered by de-adoption step 1 instead, as
in [Install each companion release](#install-each-companion-release). A failed
OpenSearch upgrade that changed the engine version cannot be rolled back; see
[Incidents and escalation](#incidents-and-escalation).

### Recover a pending release

A `pending-install`, `pending-upgrade` or `pending-rollback` status means a Helm
transaction started and has not recorded its end. While its writer runs that is
normal. The engine refuses every run, before any write, while one of the ten
releases is pending.

1. List the queued and running writers as in step 1 of
   [the read-only checks](#read-only-checks-before-adoption). If one is running,
   let it finish and read the status again. Do not cancel a run that is inside a
   Helm transaction.
2. Read the release's history and diagnostics as for
   [a failed release](#recover-a-failed-release). The `updated` time of the
   pending revision, and the deploy record of the run that wrote it, show which
   transaction it was.
3. Recovering is safe only when all of these hold: no writer is queued or running;
   the run that wrote the pending revision has ended, or the revision's `updated`
   time is older than the deploy job's 330-minute limit; the latest revision is
   the pending one and the revision before it is `deployed`; and in `opensearch`,
   both revisions have the same application version. Then print the revision to
   return to with the `last-deployed` command of
   [a failed release](#recover-a-failed-release), set `REVISION` to it, and roll
   back with [Roll back to a recorded revision](#roll-back-to-a-recorded-revision).
   The rollback writes a new `deployed` revision on top of the pending one, and
   the engine reads only the latest. Dispatch an apply again; in live mode its
   record must show the no-op.
4. Stop and escalate otherwise: a `pending-install` (there is no earlier revision;
   for a companion, use de-adoption step 1), a revision before the pending one
   that is not `deployed`, a writer that may still hold the release, or an
   OpenSearch version change. Never edit or delete a release record to clear a
   pending state.

### Roll back to a recorded revision

The revisions to return to are in the record of the run that changed them: each
release's `revisionBefore`, with `helm history` as the check. The artifact is kept
for 90 days, so copy what a rollback needs into the private record. Roll back only
after diagnosing what failed, and only to a revision whose values, images and
external dependencies are still usable.

A rollback does not change `main`, and the next Helmfile apply, scheduled or
manual, reconciles every release to `main` again: it applies the rolled-back
release's diff and so undoes the rollback. Pair every rollback with one of these
before it:

- **Pause the schedule** (`DEMO_SCHEDULE_ENABLED=false`, as in the read-only
  checks) and dispatch nothing until `main` holds the fix. This is the only choice
  for a Jaeger or HotROD image in `digest` mode, which every run resolves from
  upstream again; a manual run can pin the previous tags with `jaeger_image_tag`
  and `hotrod_image_tag`.
- **Revert the commit on `main`** in a reviewed pull request, so that the next
  apply finds an empty diff for the rolled-back release.

Pause, let queued and running writers finish, and copy the record of the run to
roll back (as after an install). List the releases it changed and their earlier
revisions:

<!-- rehearse: recorded-revisions -->
```bash
jq -r '.releases[] | select(.changed != false) | [.id, .revisionBefore, .revisionAfter, .statusAfter] | @tsv' \
  "$RECORD/runs/$RUN_ID/deploy-record.json"
```

Roll back each listed release in the reverse of the record's order, OTel before
OCI. Set `ns`, `release` and `REVISION` (its `revisionBefore`) and run the
following. It refuses a revision that is not in the history, and in `opensearch`
one with another application version, because that would be a downgrade:

<!-- rehearse: rollback -->
```bash
if [[ -n "${ns:-}" && -n "${release:-}" && -n "${REVISION:-}" ]] &&
   helm history "$release" --namespace "$ns" -o json > "$RECORD/rollback-$ns-$release-history.json" &&
   jq -e --arg rev "$REVISION" --arg ns "$ns" '
     (map(select((.revision | tostring) == $rev)) | length) == 1 and
     ($ns != "opensearch" or (map(select((.revision | tostring) == $rev))[0].app_version == last.app_version))' \
     "$RECORD/rollback-$ns-$release-history.json" >/dev/null; then
  helm rollback "$release" "$REVISION" --namespace "$ns" --wait=watcher --timeout 60m
  diff <(helm get manifest "$release" --namespace "$ns" --revision "$REVISION") \
       <(helm get manifest "$release" --namespace "$ns") && echo "Revision $REVISION is live again"
else
  echo "Stop: set ns, release and REVISION; the revision must be in the history, and in opensearch keep the version" >&2
  false
fi
```

The release must be `deployed` at a new revision whose manifest is the one of
`$REVISION`. Then verify the stack's health and compare the identities with a
capture taken before the rollback. Restore supplemental objects and fingerprint
keys only from reviewed captures. Record the rollback, its reason and how `main`
was paired with it.

### Repair an immutable StatefulSet field

An upgrade that changes a StatefulSet field Kubernetes does not allow to change,
for example `podManagementPolicy`, `serviceName` or the selector, fails with a
`Forbidden` error and leaves the release `failed`. No live-mode apply can reach
this, because its diff must be empty; it comes from a digest-mode apply, for
example of a chart bump. The repair removes the StatefulSet object only, keeps its
Pod and claim, and lets Helmfile create it again with the new spec. Treat any
change to the claim templates as a storage change that needs explicit approval
([deployer access](deployer-access.md#what-it-does-not-grant)). The steps
below are for `opensearch/opensearch-cluster-single`, the only StatefulSet of the
ten releases. In `live` mode the engine refuses a failed release, and the refused
change never reached the StatefulSet, which keeps its earlier spec: recover with
[Recover a failed release](#recover-a-failed-release) instead of these steps.

Confirm the cause:

<!-- rehearse: statefulset-diagnose -->
```bash
helm status opensearch --namespace opensearch -o json | jq -r '.info.status + ": " + .info.description'
```

Record the data claim and the StatefulSet's claim retention. Deleting the object
is safe only while the claim is `Bound`, has no owner, and the retention policy
for deletion is `Retain`, the default:

<!-- rehearse: statefulset-claim -->
```bash
claim=$(kubectl --namespace opensearch get pod opensearch-cluster-single-0 -o json | jq -er '
  [.spec.containers[] | select(.name == "opensearch") | .volumeMounts[]
   | select(.mountPath == "/usr/share/opensearch/data") | .name] as $mounted
  | .spec.volumes[] | select(.name | IN($mounted[])) | .persistentVolumeClaim.claimName')
kubectl --namespace opensearch get persistentvolumeclaim "$claim" -o json |
  jq '{name: .metadata.name, uid: .metadata.uid, volume: .spec.volumeName, phase: .status.phase,
       owners: (.metadata.ownerReferences // [])}' > "$RECORD/opensearch-claim-before.json"
kubectl --namespace opensearch get statefulset opensearch-cluster-single -o json |
  jq '{uid: .metadata.uid, retention: (.spec.persistentVolumeClaimRetentionPolicy // {})}' \
  > "$RECORD/opensearch-statefulset-before.json"
cat "$RECORD/opensearch-claim-before.json" "$RECORD/opensearch-statefulset-before.json"
```

Delete the StatefulSet with orphan propagation. The garbage collector removes the
owner reference from the Pod, which keeps running on its claim. The deployer role
grants this `delete`:

<!-- rehearse: statefulset-orphan -->
```bash
if jq -e '.phase == "Bound" and .owners == []' "$RECORD/opensearch-claim-before.json" >/dev/null &&
   jq -e '(.retention.whenDeleted // "Retain") == "Retain"' "$RECORD/opensearch-statefulset-before.json" >/dev/null; then
  kubectl --namespace opensearch delete statefulset opensearch-cluster-single --cascade=orphan --wait=true
  kubectl --namespace opensearch get pod opensearch-cluster-single-0 -o json |
    jq -e '(.metadata.ownerReferences // []) == [] and .status.phase == "Running"'
else
  echo "Stop: the data claim is not Bound and unowned, or the StatefulSet would delete its claims" >&2
  false
fi
```

Recreate it through Helmfile: dispatch an apply, as in
[Adopt and verify the no-op](#adopt-and-verify-the-no-op). In `digest` mode the
engine syncs the failed release, and Helm creates the missing StatefulSet with the
new spec. The new StatefulSet adopts the running Pod when its selector still
matches the Pod's labels, and its rolling update replaces the Pod on the same
claim if the pod template changed. If the new selector does not match, the Pod
stays orphaned and the new StatefulSet cannot create a Pod of the same name: the
administrator deletes the orphaned Pod, which the deployer role cannot, and the
StatefulSet creates it again on the same claim:

<!-- not-rehearsed: the rehearsal's change keeps the selector, so its Pod is adopted and this branch does not occur -->
```bash
kubectl --kubeconfig "$ADMIN_KUBECONFIG" --context "$ADMIN_CONTEXT" --namespace opensearch \
  delete pod opensearch-cluster-single-0
```

Then verify that the StatefulSet is ready, the release `deployed`, and the claim
the same object bound to the same volume:

<!-- rehearse: statefulset-verify -->
```bash
kubectl --namespace opensearch rollout status statefulset/opensearch-cluster-single --timeout=10m
helm history opensearch --namespace opensearch --max 1 -o json | jq -e '.[0].status == "deployed"'
kubectl --namespace opensearch get persistentvolumeclaim "$(jq -r .name "$RECORD/opensearch-claim-before.json")" -o json |
  jq '{name: .metadata.name, uid: .metadata.uid, volume: .spec.volumeName, phase: .status.phase,
       owners: (.metadata.ownerReferences // [])}' > "$RECORD/opensearch-claim-after.json"
diff "$RECORD/opensearch-claim-before.json" "$RECORD/opensearch-claim-after.json" && echo "The data claim is unchanged"
```

Never delete the claim, and never use `--cascade=foreground` or `background`,
which delete the Pod.

### De-adopt companion releases

Use this to roll back companion ownership, and to recover from a first companion
install that left failed revision 1. Pause the schedule first, as in
[the read-only checks](#read-only-checks-before-adoption), and let queued and
running writers finish. Repeat the steps for each namespace `$ns` to de-adopt, in
the reverse of the adoption order. `$RECORD` holds `adopted.json`, written while
the ownership patches were applied.

1. Remove only the stored release records. List them first, by name and status,
   without their contents. The objects of the release are not touched:

   <!-- rehearse: deadopt-records -->
   ```bash
   kubectl --namespace "$ns" get secret --selector owner=helm,name=demo-resources \
     --output custom-columns=NAME:.metadata.name,STATUS:.metadata.labels.status
   kubectl --namespace "$ns" get secret --selector owner=helm,name=demo-resources \
     --output jsonpath='{range .items[*]}{.metadata.name}{"\n"}{end}' > "$RECORD/demo-resources-records-$ns.txt"
   ```

   Review the list, then delete each record by its exact name. The deployer role
   cannot delete a collection of Secrets, so a selector delete is refused. Helm
   must then report the release not found:

   <!-- rehearse: deadopt-delete -->
   ```bash
   while IFS= read -r secret; do
     if [[ "$secret" =~ ^sh\.helm\.release\.v1\.demo-resources\.v[0-9]+$ ]]; then
       kubectl --namespace "$ns" delete secret "$secret"
     else
       echo "Skipped a name that is not a demo-resources release record: $secret" >&2
     fi
   done < "$RECORD/demo-resources-records-$ns.txt"
   helm history demo-resources --namespace "$ns" --max 1 2>&1 | grep 'release: not found'
   ```

   For a failed first install, stop here when the ownership metadata should stay
   for a fresh install.

2. Plan the metadata removal read-only and review it. It removes exactly the fields
   that the record says were added. It refuses an object with another UID, an
   ownership value that changed since adoption, and a record that names anything
   outside the allowlist:

   <!-- rehearse: deadopt-plan -->
   ```bash
   python3 scripts/demo/companion_ownership.py --kubeconfig "$KUBECONFIG" --context "$CONTEXT" \
     --namespace "$ns" --de-adopt "$RECORD/adopted.json" > "$RECORD/de-adoption-$ns.json"
   jq -r '.[] | "\(.kind)/\(.name): \(.patch | length) operation(s)"' "$RECORD/de-adoption-$ns.json"
   ```

3. Apply it. The planner must then print only empty patches:

   <!-- rehearse: deadopt-apply -->
   ```bash
   bash scripts/demo/apply-ownership-patches.sh "$RECORD/de-adoption-$ns.json" "$KUBECONFIG" "$CONTEXT"
   python3 scripts/demo/companion_ownership.py --kubeconfig "$KUBECONFIG" --context "$CONTEXT" \
     --namespace "$ns" --de-adopt "$RECORD/adopted.json" | jq -e 'all(.[]; .patch == [])'
   ```

4. Compare each object with its source at the recorded configuration commit, from
   a clean checkout of that commit. Adoption changes metadata only, so no
   difference is expected:

   <!-- rehearse: deadopt-compare -->
   ```bash
   python3 scripts/demo/companion_values.py > "$RECORD/sources.json"
   jq -c --arg ns "$ns" '.companions[$ns].objects[]' "$RECORD/sources.json" |
     while IFS= read -r object; do
       kubectl --namespace "$ns" diff --filename - <<<"$object" || echo "Review the difference above"
     done
   ```

   Apply a source only after reviewing a reported difference:

   <!-- not-rehearsed: runs only after a reviewed difference, and the rehearsal finds none -->
   ```bash
   kubectl --namespace "$ns" apply --filename reviewed-object.json
   ```

Step 1 needs `list` and `delete` on Secrets in the namespace, which the deployer
role grants without `deletecollection`. Kubernetes cannot limit a list to Helm
records, so that access covers every Secret in the namespace. Steps 2 to 4 need
named `get` and `patch` on the allowlisted objects. Only after every adopted
namespace is de-adopted may a run use `engine=scripts` again.

### Incidents and escalation

- **Diagnostics first.** Before any retry, rollback or de-adoption, keep the
  run URL and its log, the run's `deploy-record.json` in `$RECORD/runs`, the
  `helm history` and `helm status` of each affected release, the events and Pods
  of its namespace, an identity capture (`STEP=incident-<UTC time>`) and, when the
  render is involved, a drift report. Do not delete or edit release records,
  Pods or other objects to clear a state, except as the steps above say.
- **Stop automation.** Keep or set `DEMO_SCHEDULE_ENABLED` to `false` and
  dispatch nothing until the recovery is decided. Let a running writer finish.
- **Escalate** to the cluster administrator and the maintainers when a step needs
  access the deployer role does not grant, a PVC UID or binding or a Service
  ClusterIP changed, the recovery would downgrade OpenSearch or Dashboards, a
  pending release may still have a writer, the drift report finds anything
  unexplained or missing, or the ingress controller or cert-manager is involved
  (both are externally owned). An OpenSearch downgrade, namespace deletion and
  PVC deletion are never rollback steps; trace data is disposable, and a reset
  needs its own approval ([reset the trace data](releases.md#reset-the-trace-data)).
- **Evidence before automation resumes** (the schedule switch, or the next
  dispatch after an incident): every release `deployed`; a run whose record shows
  the expected result, which in live mode is the no-op of the adoption step; an
  identity comparison with the capture taken before the incident whose every
  difference is explained; a drift report that exits 0; both stacks healthy with
  fresh traces; and `0 difference(s)` from both access checks when access was
  involved.
- **Where it goes.** The run URLs, records, captures, reports and decisions go in
  the private release record, and a dated, sanitized summary of the outcome in
  the [migration plan's evidence](MIGRATION.md#current-evidence-and-unresolved-gates).
  Never record production state in the changelog or the README: a merge is not a
  deployment. Keep OCIDs, node names, IP addresses and the ingress controller's
  version out of the repository.

Verified initial adoption unlocks guarded automation. Final acceptance requires
successful scheduled refresh and reviewed Renovate chart update, a healthy no-op
apply, at least 24 hours of observation, and verified documentation/release
procedures. Post-adoption refactors then merge before core assets and legacy
fingerprint state are retired.
