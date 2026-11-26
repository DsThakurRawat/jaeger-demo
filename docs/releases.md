<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Hosted-demo releases

For the Helmfile candidate, run `make validate` before submitting a configuration change. It compares the six application releases to the script-era inputs and checks both application and supplemental resources against the target Kubernetes and chart schemas. The dependency check pairs script and Helmfile chart updates during the transition. Production continues to run the extracted workflow until operator acceptance. [Application validation](helmfile-applications.md) · [Adoption rehearsal](helmfile-companions.md)

A deployment release is a configuration commit plus the chart and tool versions,
Jaeger and HotROD image references and digests, and verification evidence.
`jaegertracing/jaeger` publishes the product images. This repository deploys them;
it does not create a second Jaeger product version or image-publishing pipeline.

## Current stage

The extraction retains the existing shell scripts and Python reconciler.
Manual deployments use a reviewed GitHub environment. The
[script schedule](cutover.md#review-scheduled-scope) runs the same `deploy` job
and reconciler in its own environment, `demo-scheduled`, behind the
`DEMO_SCHEDULE_ENABLED` switch. The same job can run the
[Helmfile engine](#helmfile-engine) instead of the scripts. It is off by default:
scheduled runs take the scripts until `DEMO_DEPLOY_ENGINE` is `helmfile`, and once a
companion release is installed the scripts must not run at all
([the engine switch](#the-engine-switch),
[Helmfile conversion](https://github.com/jaegertracing/jaeger-demo/issues/1)). The
generated bundle and read-only `plan` operation of the earlier design are retired
([migration plan](MIGRATION.md#execution-contract)).

The current workflow supports these inputs:

| Input | Use |
| --- | --- |
| `expected_config_sha` | Full reviewed configuration SHA; activates restricted Stage 1 handover guards. Required with `engine=helmfile` |
| `demo_stack` | `oci`, `otel`, or `all`. Scripts only |
| `otel_deploy_scope` | Component selection; despite its name it also selects OCI `jaeger` or `monitoring`. Scripts only |
| `jaeger_image_tag` | Explicit Jaeger snapshot tag |
| `hotrod_image_tag` | Explicit HotROD snapshot tag |
| `image_tag` | Shared fallback when an independent tag is omitted |
| `engine` | `scripts` (default) or `helmfile` |
| `helmfile_operation` | Helmfile only: `apply` (default) or `install-companion` |
| `companion_namespace` | Helmfile `install-companion` only: `default`, `jaeger`, `opensearch` or `otel-demo`; otherwise `none` |

Supply each stack's existing published commit tags for the first handover, using
separate OCI and OTel dispatches when they run different images. Do not use `latest`
or leave image selection implicit for that reviewed run. Default image selection
resolves upstream `jaegertracing/jaeger` main, never this configuration repository's
SHA. Jaeger and HotROD tags can differ because they are published independently.
Record the resolved digests even while the scripts still render tag references.
Do not convert image reference syntax during the ownership transition.

A manual run that leaves the tags empty requires a published snapshot for upstream
`main` HEAD. When there is none, typically because the upstream CI run for that
commit did not finish, each selected Jaeger component fails before its release is
changed, and the error names the cause. The run continues with the other selected
components, as after any component failure, so a run that also selects a component
other than Jaeger may change the cluster even though it fails. Dispatch again with
`image_tag=latest`, which the resolver verifies and turns into the newest published
commit tag, or with an explicit published commit tag. A missing explicit tag fails
without this hint. Only the daily schedule falls back to the newest published tag
on its own.

The script reconciler can deploy OTel before OCI and continue independent
components after a failure. The stricter OCI-before-OTel failure gate is a Helmfile
requirement, not an extraction guarantee. Existing public smoke checks also do not
replace the complete health acceptance below.

## Release record

Before a production transition, prepare a private operational record containing:

- The merged configuration commit and source-extraction cutoff.
- The exact workflow inputs and selected components/releases.
- Jaeger chart source commit, other chart versions, resolved dependency archives
  and checksums, tool versions and checksums, and pinned Action revisions.
- Independent Jaeger/HotROD image commits, exact references and resolved digests.
- Prior Helm revisions for each affected release, supplemental resource identities
  and captured specs, PVC bindings/capacities, ingress routes and TLS references.
- Rendered changes, explained live drift, a fresh functional baseline, verification
  steps and the exact rollback scope.
- The human-verification decision, time, and hash of the frozen record.

Freeze configuration and all consequential inputs between verification and
execution. If they change, regenerate the record and obtain verification for the
changed consequential inputs. A moving tag or unpinned tool cannot satisfy this
contract merely because its final value appears in a job log. This is an
operational gate; no tool rejects drift automatically. Helmfile adds a live drift
report.

Production and credential-free chart tests pin Helm v4.3.2, the version observed
in the November 17, 2026 core deployment writer. Compare fresh release revisions
and timestamps with that writer evidence before handover; the Cloud Shell client
version is not evidence of the last release writer. Freeze and verify the other
tool versions and checksums in the operational record as well.
`scripts/demo/oci-cli-constraints.txt` pins OCI CLI 3.96.3 and the dependency
versions observed in the same writer, and `scripts/demo/oci-cli-hashes.txt`
repeats those pins with the SHA-256 of every file PyPI publishes for them. Before
any step holds the OCI API key, the workflow installs that file with
`pip install --require-hashes`, so a file or dependency that is not listed stops
the run. The OKE setup action's own pip command then runs under the same
constraints with no package index, so it can only confirm that installation. After
setup, `python scripts/demo/verify_oci_cli.py` checks every pinned distribution and
the CLI executable's version, including when the action's installed marker skips
installation. A rejected tool check cannot start reconciliation.
Credential-free CI runs the same installation and check. The trace-reset and
volume-expansion workflows install, constrain and check the same way after
checkout and before they verify the deployment identity's access.
[Deployer access](deployer-access.md#oci-cli-installation) lists what hash
checking does not cover, chiefly the PyYAML source build, and which steps receive
the API key.
Kubectl follows the live cluster's Kubernetes version; record its exact version
and checksum along with the runner image used for handover. Supply
`expected_config_sha` during the reviewed script handover. The workflow
rejects a different configuration SHA before configuring OKE access. Verify the
dispatched run's `headSha` and retain that evidence. Do not execute a different
configuration revision under an earlier approval.

With a reviewed SHA, only a manual single-stack Jaeger upgrade with explicit
commit tags is permitted. Existing deployed release state, Helm storage/namespace
and required objects are checked before application writes; the Helm commands
cannot fall back to installation. See the [handover permission checks](cutover.md#check-the-restricted-handover-path).
OTel ingress apply or certificate-read failures prevent a successful component
record.
Without a reviewed SHA, ordinary deployment and schedule selection behavior is
unchanged. Scheduling still requires its separate scope/access decision.

After the run, append its URL, actual inputs, changed releases, resulting Helm
revisions, image digests, checks and outcomes. A Helmfile run writes these itself
([deployment record](#deployment-record)). Mark it successful only after the
functional checks pass. The existing `default/jaeger-demo-deployment-revisions`
ConfigMap stores component fingerprints; it is not a complete release record or
an authoritative last-known-good image-digest store.

Keep a sanitized release summary with the configuration history. Keep detailed
operational records privately through migration acceptance and while any recorded
revision remains a supported rollback target. Confirm storage location, access and
retention before handover; expiring GitHub logs alone are insufficient. Never
publish Secret contents, credentials, raw Helm values or unreviewed configuration
payloads.

## Change and verification process

1. Open a scoped issue and PR. Run local checks and credential-free CI, update the
   affected documentation, and obtain maintainer review.
2. Merge to main. The extraction must use a history-preserving merge that retains
   both imported authored history and the seed commit. Later PRs follow the
   repository's normal merge policy.
3. Prepare and verify the frozen release record. For the initial transition use
   the checkpoints in [cutover.md](cutover.md). Routine accepted operations do not
   require repeated permission, but access changes, destructive resets and changes
   to the documented invariants need a concrete decision.
4. Deploy only the selected components, using the reviewed inputs. A failed run
   remains failed even if unrelated components succeed.
5. Verify fresh trace write/read, current generated traffic, UI and public routes,
   finite SPM calls and latency under load, Grafana, TLS and the configured analytics
   behavior on both demos. Compare namespace/release names, Service addresses and
   PVC bindings against the pre-run record.
6. Record the result. Diagnose failures before retrying or rolling back; do not
   promote an unverified result to last-known-good state.

## Daily component updates

The scheduled run is the `deploy` job of `ci-deploy-demo.yml` on `main`, in the
`demo-scheduled` environment. [The engine switch](#the-engine-switch) decides what
that job runs: `scripts/demo/deploy.py` unless the repository variable
`DEMO_DEPLOY_ENGINE` is `helmfile`, and the [Helmfile engine](#helmfile-engine)
while it is. Read the variable before you read a scheduled run, because the two
engines select, verify and record differently:

```bash
gh variable get DEMO_DEPLOY_ENGINE --repo jaegertracing/jaeger-demo
```

Under either engine the run resolves the Jaeger and HotROD snapshot images from
upstream `main` when it starts and uses the reviewed configuration on main.
Renovate proposes chart, image and dependency changes for maintainer review, with
a seven-day release-age policy and manual merges, and the next daily run applies
what was merged. Unchanged storage, monitoring and sample-application inputs retain
their current releases.

Generated trace history is disposable. No workflow or script takes or restores a
backup, and no deployment waits for one. Manual and scheduled OpenSearch upgrades
preserve the existing PVC and create no snapshot, restored volume or test Pod. If
the stored format becomes unusable, historical traces are lost. After an
explicitly approved repair, new traffic repopulates the demo. See
[OpenSearch storage maintenance](#opensearch-storage-maintenance).

A chart version and its application version can differ: a chart-only release
repackages the same OpenSearch or Dashboards version. Manual and scheduled runs
require each release to be `deployed` at its chart pin and read the
application version with `helm get metadata`, which uses the Helm release
Secret reads the upgrade already needs.

### With the scripts engine

The run calls `scripts/demo/deploy.py`, as a manual `engine=scripts` run does, and
resolves the Jaeger and HotROD snapshot tags independently. Both Jaeger releases
refresh daily. Each successful component records its fingerprint by merge-patching
only its own key in `default/jaeger-demo-deployment-revisions`; a first manual
install creates the ConfigMap.

| Component | Helm releases | Daily action and checks |
| --- | --- | --- |
| OpenSearch | `opensearch/opensearch`, `opensearch/opensearch-dashboards` | On input changes, upgrade OpenSearch on its existing PVC, wait for the StatefulSet, then upgrade Dashboards and wait for its Deployment. The Dashboards Pod is Ready only when its status API answers 200. Check the database and Dashboards versions against each chart's application version and write/read a fresh trace before recording success. The public Dashboards status is read up to 30 times, two seconds apart, while the route answers 502, 503 or 504 or the connection fails. After a Dashboards rollout, also run the [console and menu check](../examples/otel-demo/README.md#verifying-the-deployment). |
| OTel Jaeger and HotROD | `jaeger/jaeger` | Refresh published snapshots, verify collector and HotROD endpoints, generator rollout, public query and UI. Check Helm ownership and update HotROD in place. An immutable selector change needs a separate reviewed migration. |
| OTel sample application | `otel-demo/otel-demo` | On input changes, upgrade the reviewed chart and values, then verify the collector agent, frontend and load generator. After a rollout, also run the [control-interface check](../examples/otel-demo/README.md#verifying-the-deployment). |
| OCI Jaeger and HotROD | `default/jaeger` | Refresh published snapshots, verify Jaeger, HotROD and generator rollouts, public HotROD dispatch and the Jaeger services API. Check the UI during post-run verification. |
| OCI monitoring | `default/prometheus` | On input changes, upgrade the reviewed monitoring chart and values, and wait for Grafana rollout. Check the public Grafana health endpoint as part of operator verification. The next scheduled run applies a merged chart or values change unattended; to upgrade under review instead, set `DEMO_SCHEDULE_ENABLED` to `false` before merging and follow [When a change alters a release's objects](#when-a-change-alters-a-releases-objects). |

A scheduled run selects both Jaeger components and every component whose
fingerprint differs from its key in the ConfigMap, including a missing key. It
requires that ConfigMap and the existing releases, and upgrades in place only:
it installs no release and creates no namespace. Both Jaeger releases must have
Helm status `deployed`. Any other release may be `deployed` or `failed`, and a
failed one is upgraded in place. A release in a `pending-*` state is refused until
an operator repairs it. Charts install only from archives whose SHA-256 matches
`scripts/demo/chart-archives.lock.json`, checked before any cluster change. A change
that adds, removes or renames an object is applied like any other once the
deployer role covers it. See
[When a change alters a release's objects](#when-a-change-alters-a-releases-objects).

Once the schedule is enabled, a merged change to the OpenSearch and Dashboards
charts is applied in place by the next scheduled run, on the existing PVC and
without a backup. The run compares the data PVC and its bound volume before and
after the upgrade. To apply an engine upgrade under review instead, merge after
the 13:00 UTC run or pause the schedule, dispatch the manual
`otel` / `opensearch` upgrade, and let it finish. A manual upgrade takes no
backup either, and an OpenSearch engine upgrade cannot be rolled back with Helm.
Its successful result records the OpenSearch fingerprint, so the next
scheduled run does not select OpenSearch again.

An OpenSearch failure holds both dependent OTel components. Independent OCI
updates may succeed in the same run. A failed component keeps its preceding
fingerprint. Inspect each component outcome and its live Helm history before a
retry, especially when OpenSearch upgraded before Dashboards failed.

### With the Helmfile engine

A scheduled Helmfile run is an `apply` in `digest` mode. It refuses `live` mode and
installs no release ([images](#images)). An operator sees four differences from the
scripts:

- **Selection.** No fingerprint selects components. Every run applies all ten
  releases, the six application releases of the table above and the four
  `demo-resources` companions, each alone, and `helm diff` decides which ones
  change. A `deployed` release with an empty diff keeps its revision. A `failed`
  release is synced even when its diff is empty. A release in any other state
  stops the run before a write.
- **Order.** The OCI releases and the OCI checks finish before any OTel release is
  touched, and OTel never starts when OCI fails.
- **Checks.** Each stack is verified even when nothing changed, after its trace
  generator was restarted. The three OTel Certificates must be present and Ready,
  which the scripts only log, and each Jaeger and HotROD workload must run the
  image the run validated.
- **Record.** Each run, also a refused or failed one, uploads `deploy-record.json`.
  A stack's component fingerprints are patched only after its checks pass, to keep
  the scripts' ConfigMap current for a later return to them.

[What a Helmfile run does](#what-a-helmfile-run-does) lists the steps and checks,
[when a Helmfile run fails](#when-a-helmfile-run-fails) the failure cases, and
[deployment record](#deployment-record) the fields. The operator checks that the
table names after a rollout (the Dashboards console and menu check, the OTel Demo
control-interface check and the public Grafana health endpoint) do not depend on
the engine.

A merged change to the OpenSearch and Dashboards charts is applied in place by the
next scheduled run under this engine too, on the existing PVC and without a backup,
and the run requires the data claim and its volume to be unchanged afterwards. To
apply it under review instead, merge after the 13:00 UTC run or pause the schedule,
and dispatch a manual `engine=helmfile` apply. That run ignores `demo_stack` and
`otel_deploy_scope` and reconciles all ten releases
([the engine switch](#the-engine-switch)).

### Accepting the first scheduled run

Before activation, review the scope read-only with the schedule disabled. After
activation, use the first actual scheduled run to accept the deployment, and
record its run URL. The scheduled failure notification includes the run ID in
`#jaeger-operations`. Accept the run by the engine it took.

**Scripts engine.** Reconcile the six releases and five component fingerprints
with the pre-run record, and verify fresh traces, public health, TLS and storage
bindings.
[Activation and first-run criteria](cutover.md#review-scheduled-scope)

**Helmfile engine.** Six releases and five fingerprints leave out the four
companions, so accept on the run's own record. Download it and require a
scheduled digest-mode apply that succeeded, verified OCI before OTel, left all ten
releases `deployed` and patched the five fingerprints:

```bash
bash -euo pipefail <<'EOF'
: "${RUN_ID:?Set the scheduled run ID}" "${RUN_ATTEMPT:=1}"
gh run download "$RUN_ID" --repo jaegertracing/jaeger-demo \
  --name "deploy-record-$RUN_ID-$RUN_ATTEMPT" --dir "deploy-record-$RUN_ID"
jq -e --arg run "$RUN_ID" '
  .run == $run and .event == "schedule" and .outcome == "succeeded"
  and .operation == "apply" and .imageMode == "digest"
  and .verifiedStacks == ["oci", "otel"] and .generatorRestarts == ["oci", "otel"]
  and (.releases | length) == 10 and all(.releases[]; .statusAfter == "deployed")
  and (.fingerprints | length) == 5' "deploy-record-$RUN_ID/deploy-record.json"
jq -r '.releases[] | [.id, .revisionBefore, .revisionAfter, .changed] | @tsv' \
  "deploy-record-$RUN_ID/deploy-record.json"
EOF
```

The first command prints `true` and the second the revisions to compare with the
pre-run record: a release whose inputs did not change keeps its revision. The
record's `checks` hold each stack's check results with the running image
references. Then verify fresh traces in both Jaeger instances, the public routes
and their certificates, and that the OpenSearch data claim is still bound to its
volume, as for a scripts run. Keep the run URL and the record in the release
record; the artifact expires after 90 days.

## Failure handling

A manual or scheduled run of the `deploy` job that fails, is cancelled or times
out posts to `#jaeger-operations` with its status. The job is limited to
330 minutes and its reconcile step to 300, which leaves 30 minutes for setup
and the notification.

A run that ends before its job starts, for example while it waits for approval
or when a newer pending run replaces it, runs no steps and cannot report itself.
The [run observer](../.github/workflows/demo-run-observer.yml) posts to the same
channel for a deploy or smoke-check run that was cancelled, failed, timed out or
failed at startup while none of its jobs reached a runner. It stays silent when
a job did start, because that job reports itself. The observer runs from the
workflow file on `main` and does not report its own failures. It does not watch
the trace-reset and volume-expansion workflows.

The deployment, trace-reset and volume-expansion workflows share the
concurrency group `jaeger-otel-demo-opensearch-recovery` with
`cancel-in-progress: false`, so one of them runs at a time and a running writer is
never cancelled. GitHub keeps one waiting run per group: a newer waiting run
cancels an older waiting one. Do not dispatch a manual writer close to 13:00 UTC,
and do not leave a dispatched run waiting for approval across 13:00 UTC; either
can cost the scheduled run or the manual one.

### A waiting run replaced by a newer one

This is accepted behavior
([#102](https://github.com/jaegertracing/jaeger-demo/issues/102)), and
`scripts/demo/workflow_test.py` asserts the setting on all three workflows.
GitHub documents a
[`queue: max`](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax#concurrency)
setting that keeps up to 100 waiting runs per group instead of one. It is not
used, because actionlint 1.7.12, the version CI runs and the newest release on
2026-11-24, rejects the key
([rhysd/actionlint#657](https://github.com/rhysd/actionlint/issues/657)). Adopt
it in all three workflows in one change once a released actionlint accepts it.

Until then a writer that waits is cancelled as soon as another writer starts to
wait in the group. The usual case is the 13:00 UTC run waiting behind a long
manual run when someone dispatches a second writer. GitHub does not document
whether a run that waits for `demo-apply` approval already holds the group, so
treat it as holding it: other writers wait behind it, and the rule above applies.

The cancelled run changed nothing in the cluster. The run observer reports a
cancelled deploy run; a reset or volume-expansion run cancelled this way is
not reported, so check its run page after dispatching one. When the observer
reports a cancelled scheduled run:

1. Open the run and confirm that its job has no steps and that another writer
   was created while it waited. A run with steps is a different failure; follow
   the rest of this section.
2. Decide whether the update can wait a day. The next scheduled run selects both
   Jaeger components and every component whose fingerprint still differs, so the
   skipped update is applied then and no merged change is lost. If it can wait,
   nothing more is needed.
3. To apply it the same day, first list the writers that are running or waiting
   and let them finish:

   ```bash
   for workflow in ci-deploy-demo.yml ci-opensearch-reset.yml ci-opensearch-volume-expand.yml; do
     gh api --paginate \
       "repos/jaegertracing/jaeger-demo/actions/workflows/$workflow/runs?per_page=100" \
       --jq '.workflow_runs[] | select(.status != "completed") | {id, event, status, created_at, html_url}'
   done
   ```

   Then re-run the cancelled scheduled run. A re-run deploys only while `main` is
   still at the run's commit and stops at its first step otherwise. After a later
   merge, wait for the next scheduled run or dispatch the stack and scope that
   must not wait as a manual run, which needs a `demo-apply` reviewer. A re-run
   or a dispatch waits in the group like any other writer, so start it only when
   the list is empty.

What a failed run leaves behind:

- A failed component's fingerprint is not recorded, so the next run selects and
  retries it. Components that succeeded in the same run keep their new keys.
- An OpenSearch failure blocks the dependent OTel components in that run.
- An OpenSearch component that fails with `Public Dashboards status was
  unavailable after 30 attempts` had already upgraded both releases: the public
  route kept answering 502, 503 or 504, or the connection failed, for the whole
  wait. The message ends with the last status. Check the Dashboards Pod's
  readiness and the ingress before a rerun.
- A scheduled run refuses a release in a `pending-*` state, and a failed Jaeger
  release, until an operator repairs it.
- A run that stops at **Verify the deployment identity's access** changed
  nothing. The credential lacks part of `ops/rbac/deployer.yaml`; an
  administrator applies the role and reruns `check`
  ([deployer access](deployer-access.md)).
- A re-run of a scheduled run deploys only at the current `main` commit. After a
  later merge, wait for the next scheduled run or dispatch manually.

A failure leaves the schedule switch enabled, so the next scheduled run retries.
Set the repository variable `DEMO_SCHEDULE_ENABLED` to `false` to
pause future scheduled jobs while inspecting a failure. Check queued and running
writers separately and let an active Helm transaction finish before rolling back.
Preserve the failed run's diagnostics and compare actual release state with the
recorded prior revisions. Inspect a pending Helm operation before deciding how
to recover it. [Pause and resume the script schedule](cutover.md#review-scheduled-scope).

For an application release, a previously recorded revision can be restored with
`helm rollback` after verifying that its values, images and external dependencies
remain usable. Use the namespace as well as the release name because two releases
are named `jaeger`. Follow the exact recorded rollback procedure, then repeat the
functional checks. Restore supplemental resources and fingerprint state only from
reviewed pre-run captures and only after verifying the resulting deployment.

OpenSearch downgrade, `clean`, `cleanup.sh`, namespace deletion and PVC deletion
are not generic rollback procedures. Storage expansion cannot be reversed by
shrinking the PVC. Disposable trace data allows a separately approved narrow
[reset](#reset-the-trace-data); it does not authorize routine destructive maintenance.

## OpenSearch storage maintenance

Generated traces are disposable, so the repository holds no backup or restore
tooling and no deployment is gated on a recovery point
([#76](https://github.com/jaegertracing/jaeger-demo/issues/76)). Routine
maintenance preserves storage:

- **Upgrades** keep the existing data PVC. A scheduled run fails if the claim or
  its bound volume changed during the upgrade.
- **Volume expansion** is the manual
  [Expand OTel Demo OpenSearch Volume](../examples/otel-demo/README.md#opensearch-volume-expansion)
  workflow, approved by a `demo-apply` reviewer.
- **Index retention** removes Jaeger indices older than 30 days
  ([index retention](../examples/otel-demo/README.md#index-retention)).

The one destructive operation is the manual [trace reset](#reset-the-trace-data)
below. It deletes trace indices and keeps the data volume. No workflow identity
can delete a PersistentVolumeClaim. Replacing the data volume is a destructive
storage change that no workflow performs: pause the schedule, get explicit
maintainer approval, and have an administrator perform it outside the workflows.

### Reset the trace data

**Reset OTel Demo OpenSearch Trace Data**
([ci-opensearch-reset.yml](../.github/workflows/ci-opensearch-reset.yml)) deletes
the stored Jaeger traces of the OTel demo and nothing else. Use it when the trace
indices are unusable or fill the volume and the 30-day retention cannot be waited
for. It is never part of a deployment. It runs only from a manual dispatch on
`main`, waits for a `demo-apply` reviewer every time, and shares the writers'
concurrency group.

Every name it touches is a constant in
[scripts/demo/opensearch_reset.py](../scripts/demo/opensearch_reset.py). No input
selects a namespace, a Pod, a workload or an index.

| Resource | What the reset does |
| --- | --- |
| Pod `opensearch/opensearch-cluster-single-0`, container `opensearch` | Sends requests to the engine on `localhost:9200` |
| Indices named `jaeger-main-jaeger-span-<date>`, `-service-`, `-dependencies-` and `-sampling-<date>`, with the date as `YYYY-MM-DD` | Deletes each one by its exact name, one request per index |
| Deployment `jaeger/jaeger` | Restarts it and waits for the rollout |

It deletes no Kubernetes object. The StatefulSet, its Pod, the data claim and the
volume stay, and the freed space returns to that volume. Every other index stays:
the `jaeger-archive-*` indices, the Dashboards saved objects and the index
templates. It needs no access beyond the deployer role
([deployer access](deployer-access.md#what-the-deployer-role-grants)).

1. Dispatch `operation=list` first. After approval the run prints the indices a
   reset would delete and the ones it would keep, and changes nothing.

   ```bash
   gh workflow run ci-opensearch-reset.yml --repo jaegertracing/jaeger-demo --ref main -f operation=list
   ```

2. A reset is a destructive change that needs a concrete maintainer decision
   ([migration plan](MIGRATION.md#release-and-documentation-contract)). Record it,
   then dispatch `operation=reset` and type the acknowledgment exactly, with no
   quotes or extra spaces:

   ```bash
   gh workflow run ci-opensearch-reset.yml --repo jaegertracing/jaeger-demo --ref main \
     -f operation=reset -f 'acknowledgment=delete all Jaeger trace indices in opensearch/opensearch-cluster-single-0'
   ```

   Do not dispatch it close to 13:00 UTC, as for any writer.
3. The run checks the request before it configures cluster access, then checks its
   identity's access, then checks that the Pod is the live Pod of the
   `opensearch-cluster-single` StatefulSet and that the engine is the one-node
   `opensearch-cluster`. Only then does it delete. Afterwards it requires that no
   other index and no Jaeger index template disappeared.
4. It restarts `jaeger/jaeger`. Jaeger writes its index templates at startup and
   remembers the service names it has already recorded, so without a restart the
   Jaeger UI would list no services for up to 12 hours. The run then sends a fresh trace
   through the collector, reads it back, and finds its service in the service list.
5. Verify the demo as in step 5 of the
   [change and verification process](#change-and-verification-process), and record
   the run URL, the deleted index count and the decision in the release record.
   The Jaeger UI shows only traces written after the reset.

The run stops before any change when the acknowledgment differs, when the event
is not a manual dispatch from `main`, when the Pod or the engine is not the
expected one, or when the engine's listing is not what the script expects. A run
that fails after its first delete request still restarts `jaeger/jaeger` before it
reports the error, so the UI does not miss services for 12 hours; if that restart
also fails, the log says so and the Deployment needs a manual
`kubectl -n jaeger rollout restart deployment/jaeger`. A run that fails part-way
can be dispatched again: it deletes what is left and restarts Jaeger. The workflow
posts nothing to Slack and the run observer does not watch it, so watch the run.

The reset needs an OpenSearch that answers. It cannot repair an engine that does
not start, and it does not replace the data volume.

### After the backup tooling was removed

The removal changes `examples/otel-demo/deploy-all.sh`, which is a fingerprint
input of every OTel component, and drops `recovery_policy.py` from the OpenSearch
inputs. The first scheduled run after the merge therefore selects `opensearch`,
`otel-jaeger` and `otel-demo`, and upgrades `opensearch`, `opensearch-dashboards`
and `otel-demo` in place with unchanged charts and values. To do that under
review instead, pause the schedule before merging and dispatch the
`otel` / `opensearch` and `otel` / `otel-demo` upgrades manually
([merging a change](#merging-a-change)).

Two operator steps follow the merge. Neither blocks a deployment.

1. **Apply the reduced role.** `ops/rbac/deployer.yaml` no longer grants the
   `jaeger-demo-recovery` role anything that only the backup and restore tooling
   used. Runs keep passing before the file is applied, because the live role is
   wider than what a run asks for. Until then the administrator's
   `check --identity manual --user` reports the removed access as
   `must not be allowed`. When no writer workflow is running, apply the file and
   require `0 difference(s)` for both identities
   ([changing the role](deployer-access.md#changing-the-role)). The bindings are
   unchanged.
2. **Inspect what the tooling left in the cluster.** Each backup run created a
   VolumeSnapshot `jaeger-os-recovery-<run>`, a restored PersistentVolumeClaim
   `jaeger-os-restore-<run>` and a ConfigMap `jaeger-os-query-<run>` in
   `opensearch`, all labelled `jaegertracing.io/recovery-source=opensearch`, and
   a disposable Pod `jaeger-os-verify-<run>` that it removed afterwards. The first
   run also created the cluster-scoped VolumeSnapshotClass
   `jaeger-opensearch-oci-retain`. Nothing deleted the retained objects, and the
   workflow identities can no longer read them. With an administrator kubeconfig,
   list them first. These commands change nothing:

   ```bash
   kubectl get volumesnapshotclass jaeger-opensearch-oci-retain
   kubectl -n opensearch get volumesnapshots -l jaegertracing.io/recovery-source=opensearch
   kubectl -n opensearch get persistentvolumeclaims,configmaps,pods -l jaegertracing.io/recovery-source=opensearch
   kubectl get volumesnapshotcontents \
     -o custom-columns='NAME:.metadata.name,CLASS:.spec.volumeSnapshotClassName,POLICY:.spec.deletionPolicy,SNAPSHOT:.spec.volumeSnapshotRef.name,NAMESPACE:.spec.volumeSnapshotRef.namespace'
   kubectl -n opensearch get persistentvolumeclaims \
     -o custom-columns='NAME:.metadata.name,VOLUME:.spec.volumeName,CAPACITY:.status.capacity.storage,PHASE:.status.phase'
   ```

   A `NotFound` answer, or an error that the server has no such resource type,
   means that kind was never created. Deleting anything found is a separate,
   explicitly approved administrator action, because it is not reversible:

   - A restored claim `jaeger-os-restore-<run>` is a full copy of the data volume
     at the time of the test. Whether its block volume is deleted with it depends
     on the reclaim policy of the bound PersistentVolume.
   - Snapshot contents of the class `jaeger-opensearch-oci-retain` have the
     deletion policy `Retain`. Deleting the VolumeSnapshot and its content leaves
     the OCI block volume backup, which then has to be deleted in OCI. Its
     identifier is the content's `status.snapshotHandle`. That value is private
     infrastructure data: keep it out of issues, pull requests and public logs.
   - The live data claim is `opensearch-cluster-single-opensearch-cluster-single-0`.
     It carries no `jaegertracing.io/recovery-source` label. Never delete it as
     part of this cleanup.
   - Delete by exact name, one object at a time, after reading the list again.
     Record what was found, what was deleted and when in the release record.

## Helmfile engine

[scripts/demo/helmfile_deploy.py](../scripts/demo/helmfile_deploy.py) deploys the
ten releases of `helmfile.yaml` and `helmfile-resources.yaml.gotmpl` from the same
`deploy` job, identities, role and concurrency group as the scripts
([#73](https://github.com/jaegertracing/jaeger-demo/issues/73)). Its first
production use is manual adoption. The [Helmfile adoption](cutover.md#helmfile-adoption)
runbook gives the procedure: the operator shell and checks, the ownership patches
and companion installs in their order, the adoption apply and the no-op that
follows, and the recovery, rollback and de-adoption steps
([#74](https://github.com/jaegertracing/jaeger-demo/issues/74)).

### The engine switch

| Run | Engine |
| --- | --- |
| Scheduled | `helmfile` only while the repository variable `DEMO_DEPLOY_ENGINE` is `helmfile`; otherwise, and while it is unset, the scripts |
| Manual | The `engine` input, default `scripts` |

The job sets `DEPLOY_ENGINE` from these. With `scripts` the job runs exactly the
script path: the Helmfile steps are skipped and **Reconcile demo components** runs
`deploy.py`.

Leave `DEMO_DEPLOY_ENGINE` unset until manual adoption and a verified no-op apply
([migration plan](MIGRATION.md) phase 6). Before setting it to `helmfile`, merge the
reviewed change that sets [helmfile/images.yaml](../helmfile/images.yaml) to
`mode: digest`: a scheduled run refuses `live` mode, so with `live` every scheduled
run would stop at **Resolve and validate images**.

Going back to the scripts is not a switch flip once a companion is installed. The
scripts `kubectl apply` the supplemental objects that the companion releases then
own. First pause the schedule and let every queued or running writer finish
([failure handling](#failure-handling)), then complete
[De-adopt companion releases](cutover.md#de-adopt-companion-releases) for each
adopted namespace, and only then unset `DEMO_DEPLOY_ENGINE` or dispatch with
`engine=scripts`. A verified Helmfile apply patches the five component
fingerprints in `default/jaeger-demo-deployment-revisions` one key at a time, as
`deploy.py` does, so the first script run after that does not select components
only because Helmfile deployed them.

A manual Helmfile run requires `expected_config_sha`, and the first step refuses
the run without it. A reviewer approves it in `demo-apply` as usual:

```bash
gh workflow run ci-deploy-demo.yml --repo jaegertracing/jaeger-demo --ref main \
  -f engine=helmfile -f expected_config_sha="$(git rev-parse origin/main)"
```

The Helmfile engine ignores `demo_stack` and `otel_deploy_scope`: every apply
reconciles all ten releases, and helm-diff decides which ones change. The first
install of a companion is a separate dispatch for one namespace, after its
reviewed ownership patches ([cutover](cutover.md#apply-the-ownership-patches)):

```bash
gh workflow run ci-deploy-demo.yml --repo jaegertracing/jaeger-demo --ref main \
  -f engine=helmfile -f expected_config_sha="$(git rev-parse origin/main)" \
  -f helmfile_operation=install-companion -f companion_namespace=default
```

It runs `release_phases.install_args`: a plain `helm install` of `demo-resources`,
not atomic and without `--take-ownership`, and only while Helm reports that release
absent. Before it, with reads only, it refuses the run while any release in the four
namespaces is `pending-*`, or present by name but not `deployed` although Helm's
listing omits it (the `unknown` state; a companion that is simply not installed yet
is fine), and unless every object that
[charts/demo-resources/allowlist.yaml](../charts/demo-resources/allowlist.yaml) lists
for that namespace exists and carries exactly the reviewed ownership: the
`app.kubernetes.io/managed-by: Helm` label, the `meta.helm.sh/release-name:
demo-resources` and `meta.helm.sh/release-namespace` annotations and the keep
policy ([companion adoption](helmfile-companions.md)). Helm would otherwise create
a missing object, with a new UID and, for a Service, a new ClusterIP. It then
renders the companion offline with the install's own chart and values and compares
every object with the live one, by GET requests only, using the classification of
the [live drift report](helmfile-applications.md#live-drift-report): an object that
is missing, differs in a way the report cannot explain, or was never adopted
refuses the install. A changed generator pod template is such a difference. A
companion install selects, validates and reads no image (the workflow skips
**Resolve and validate images** and the record says `not used: install-companion`)
and restarts nothing, so no pod template changes. A scheduled run never installs.

### Images

[helmfile/images.yaml](../helmfile/images.yaml) sets the image mode.

- **`live`**, the mode of ownership adoption. Reconcile reads the Jaeger and HotROD references
  of both stacks from the running Deployments with `live_observation.py` and
  renders each one exactly, as a tag. A reference that is not
  `docker.io/<repository>:<tag>` is refused, because any other form would change
  the pod template. The record keeps each reference with its registry digest and
  source commit, and records compatibility as `live: not revalidated`. A scheduled
  run refuses this mode, and so does a manual run with an image tag input. Because
  a live-mode apply is adoption, it first runs the read-only `helmfile diff
  --detailed-exitcode` of every release with the apply's own values and flags, and
  refuses the run if any release would change (or is not `deployed`, which Helmfile
  reports as a change). The diff output stays out of the log.
- **`digest`**, the committed mode since ownership adoption was verified. Its first
  apply changes the Jaeger and HotROD pod templates of both stacks from tag to
  `tag@sha256` references, so those four Deployments roll once. **Resolve and
  validate images** selects the images for the upstream `main` commit that **Resolve
  upstream Jaeger main** already exported as `MAIN_SHA`, with the image tag inputs,
  so the summary and the selection name one commit. It renders both Jaeger releases
  with `helmfile template` and no cluster, and runs the Jaeger configuration gate.
  An incompatible image fails that step, before any step holds the OCI API key.
  The step prints each candidate's public reference, digest and source commit before
  the gate runs, and on a rejection writes `deploy-record.json` and the step summary
  itself, naming the rejected stack, the candidate references and the exception type,
  never rendered values. Reconcile applies only a selection whose compatibility
  receipt matches it and the configuration commit.

### What a Helmfile run does

Before the first step that holds the OCI API key:

1. **Prepare the pinned Helmfile toolchain** verifies Helm 4.3.2, Helmfile 1.8.4,
   helm-diff 3.15.15 and kubeconform against `scripts/demo/helmfile-tools.lock.json`.
2. **Prepare the pinned charts** builds the Jaeger chart, fails unless its checkout
   is at `JAEGER_CHART_COMMIT` of `scripts/demo/jaeger-chart.sh`, checks its
   dependency archives, and writes `.helmfile-locked.yaml` from archives that match
   `scripts/demo/chart-archives.lock.json`. Its Helm calls get no kubeconfig.
3. **Resolve and validate images**, as above.

**Reconcile demo components** then runs `helmfile_deploy.py reconcile`, which uses
the pinned tools for every Helm and Helmfile call:

1. Refuses a run that is not a schedule or a dispatch on `main`, whose checkout is
   not the workflow revision, or whose operation, namespace or image inputs do not
   fit the rules above. It also refuses `HELM_DRIVER` other than Secrets, every
   Kubernetes connection variable Helm reads (`HELM_KUBECONTEXT`, `HELM_NAMESPACE`,
   `HELM_KUBEAPISERVER`, `HELM_KUBETOKEN`, `HELM_KUBEASUSER`, `HELM_KUBEASGROUPS`,
   `HELM_KUBECAFILE`, `HELM_KUBETLS_SERVER_NAME`,
   `HELM_KUBEINSECURE_SKIP_TLS_VERIFY`), `HELM_MAX_HISTORY`, and any
   `HELMFILE_KUBE_CONTEXT`, `HELMFILE_NAMESPACE`, `HELMFILE_ENVIRONMENT` or
   `HELMFILE_HELM_BINARY`, as `diff` does. It checks again that `.helmfile-locked.yaml` is current, that each archive
   it names still matches the lock and that the Jaeger chart is at its pinned commit.
2. Reads, without writing: on an apply the image values and
   `default/jaeger-demo-deployment-revisions`, which it requires; one `helm list`
   per release namespace; `helm history --max 1` for each selected release, which
   must agree with the listing in status and revision; for a companion install the
   checks above; and the OpenSearch data claim and volume.
3. Runs `release_phases.run`: a release in any state but `deployed` or `failed`,
   or absent without a requested first install, stops the run here, before any
   write. Then the OCI releases (`default/prometheus`, `default/jaeger`,
   `default/demo-resources`), the OCI checks, the OTel releases in `needs` order
   with their companions last, and the OTel checks. Each release runs alone:

   ```bash
   helmfile -f .helmfile-locked.yaml --state-values-file <images> \
     -l namespace=<ns>,name=<name> --skip-deps <apply|sync> --concurrency 1 --reset-values \
     --sync-args="--install=false --create-namespace=false" \
     --diff-args=--allow-unreleased=false --suppress-secrets --suppress-diff
   ```

   A companion uses `-f helmfile-resources.yaml.gotmpl` with the values of
   `companion_values.py`. A `deployed` release is applied, so an empty diff changes
   nothing; a `failed` one is synced, even with an empty diff. Each release must be
   `deployed` afterwards, or the run fails there. There is no global
   `--rollback-on-failure`: Helmfile passes it to Helm only for the two Jaeger
   releases, which set `atomic: true`.
4. On an apply in `digest` mode only, restarts the stack's trace generator and waits
   for it before the stack's checks, as the scripts do until
   [#2](https://github.com/jaegertracing/jaeger-demo/issues/2). A `live` mode apply
   and a companion install are adoption steps and restart nothing.
5. Verifies each stack even when nothing changed, and records it as verified as soon
   as its checks pass. Right after the rollouts of an apply, each Jaeger and HotROD
   Deployment of the stack must run exactly the expected reference, and each of its
   ready Pods one of that image's registry digests (`live_observation.py`): the
   validated selection in `digest` mode, the references read at the start in `live`
   mode. A mismatch fails the stack before it is recorded or its fingerprints are
   patched. OCI: rollouts of `jaeger`, `jaeger-hotrod`, `trace-generator`
   and `prometheus-grafana`, then `stack_health.py --stack oci`. OTel: rollouts of
   OpenSearch, Dashboards, Jaeger, HotROD, the generator, the collector agent, the
   frontend and the load generator; the OpenSearch data claim and volume unchanged;
   `verify_trace.py` with the chart versions of `helmfile.yaml`; the Certificates
   `jaeger/jaeger-demo-tls`, `opensearch/opensearch-demo-tls` and
   `otel-demo/otel-demo-tls` present and Ready; then `stack_health.py --stack otel`.
   The Certificate names come from the TLS Secrets of the issuer-annotated Ingresses
   in the companion sources. This is stricter than the scripts, which only log
   Certificate readiness: a missing or unready one fails the run. The `default`
   Certificate is not checked, because the deployer role lists Certificates only in
   the OTel namespaces. The health checks are retried up to ten times, 30 seconds
   apart. OTel never starts when OCI fails.
6. On an apply, after a stack's checks pass, patches that stack's component
   fingerprints.

To see what a run would change, run the same preparation and then the read-only
`diff` with a kubeconfig for the cluster. Like reconcile, it first checks that
`.helmfile-locked.yaml` is current and its archives match the lock. It reports a
release that is not `deployed` as changed, because the apply would sync it:

```bash
python3 scripts/demo/helmfile_deploy.py prepare
python3 scripts/demo/helmfile_deploy.py images   # digest mode only
python3 scripts/demo/helmfile_deploy.py diff
```

The **Helmfile deploy driver rehearsal** CI job runs the driver on a kind cluster
as a user bound to `ops/rbac/deployer.yaml`, with synthetic application charts,
the real `demo-resources` chart and stub image selection, registry lookup, health
and trace checks. Its live-mode pass reads fixture Jaeger and HotROD Deployments and
runs the production rollout, storage and Certificate checks. On a second cluster the
same job runs the adoption runbook's marked commands verbatim, through the
deployer kubeconfig the runbook writes
([how they are rehearsed](cutover.md#how-the-commands-run-and-how-they-are-rehearsed)).

### When a Helmfile run fails

- **Refused** (the record's `outcome`): nothing was written. Fix the input or the
  state the error names. A release in a `pending-*` or `unknown` state, or a
  missing application release, needs an operator before any run continues
  ([recover a pending release](cutover.md#recover-a-pending-release)). A listing
  and history that disagree mean another writer was active: let it finish and run
  again.
- **The live image read or the OpenSearch binding read refuses**, for example while
  a Deployment is still rolling out or a Pod is not on its registry digest: this is
  operator repair under the adoption runbook
  ([incidents and escalation](cutover.md#incidents-and-escalation)). Do not fall
  back to the scripts to get past it.
- **Failed** after a write: the releases before the failure were reconciled and
  later ones were not touched. A failed OCI check leaves every OTel release as it
  was. A Jaeger release that fails rolls back by itself (`atomic`); any other
  failed release stays `failed`. In `digest` mode the next run syncs it. In `live`
  mode the next apply is refused, because its read-only diff reports a release
  that is not `deployed` as changed; return the release to its last deployed
  revision first ([recover a failed release](cutover.md#recover-a-failed-release)).
  An upgrade that fails on an immutable StatefulSet field needs
  [its own repair](cutover.md#repair-an-immutable-statefulset-field).
  Fingerprints are patched only for a stack that passed its checks.
- **A failed first companion install** leaves revision 1 `failed`. Recover with
  [De-adopt companion releases](cutover.md#de-adopt-companion-releases) step 1,
  then install again.
- To return to the scripts, follow [the engine switch](#the-engine-switch): pause,
  drain the writers and de-adopt first. The general rules above (Slack
  notification, rollback by recorded revision, no OpenSearch downgrade) apply
  unchanged. A rollback to a revision a run recorded, and how it must be paired
  with the schedule or a revert because the next apply re-applies `main`, is in
  [roll back to a recorded revision](cutover.md#roll-back-to-a-recorded-revision).

### Deployment record

Every Helmfile reconcile, including one that fails or is refused, and an image gate
rejection, appends a
summary to the run page and uploads `deploy-record.json` as the artifact
`deploy-record-<run id>-<attempt>`, kept for 90 days. It holds the configuration
commit and upstream Jaeger `main`, the image mode, each stack's Jaeger and HotROD
reference, source commit and digest, the compatibility receipt or `live: not
revalidated`, the tool versions and archive SHA-256 of the toolchain lock, the
chart archive SHA-256s and Jaeger chart commit, each selected release's status and
revision before, method, revision after and whether it changed, the stack check
results with the running image references, the generator restarts, the patched
fingerprints and the outcome. When Helm's history cannot be read after a write,
the revision after and `changed` are recorded as unknown (`null`), never as a
change. A refusing adoption gate is recorded with its name (`live-diff` or
`companion-drift`), the release, and for a companion the identity and class of each
refusing object. A rejected image candidate is recorded as the rejected stack, the
candidate references and digests and the exception type, also when **Resolve and
validate images** fails and reconcile never runs. A
failure is recorded as the operation, release or stack, the exception type and a
fixed message, never the command line, command output or an endpoint. It holds no
Helm value, Secret, diff or storage identifier: anyone who can read the
repository's Actions can read both. On an error the command prints only its type;
the commands' own output above it in the log has the detail. Copy what a rollback needs into the private release record before the
artifact expires.

## Helmfile acceptance

The deploy workflow's [Helmfile engine](#helmfile-engine) runs `helmfile apply` in
place of the scripts, with compatibility checks before mutation, failed-release
retry with empty diffs, pending-state blocking and OCI verification before OTel
mutation. It adds no bundle and no second identity. Its first
ownership adoption freezes live chart/config/image references. A verified no-op
apply follows adoption; digest-reference promotion is a separate deployment.

Verified manual adoption unlocks guarded automation. Final cutover acceptance
requires a successful scheduled refresh, a reviewed Renovate chart bump, another
healthy no-op result, and at least 24 hours of observation. Core removal waits for
that acceptance and the separate post-adoption refactors to merge.

No workflow deploys through the in-cluster executor, and its code and documents
are not part of this repository. The cluster-side removal of its objects is
described in [executor retirement](executor-retirement.md).

<a id="merging-a-change-to-guarded-files"></a>

## Merging a change

A merged change takes effect at the next scheduled run, which deploys the
configuration on `main` when it starts. No image has to be published or installed
first. When a change must not go out unattended, set `DEMO_SCHEDULE_ENABLED` to
`false` before merging, deploy it with a manual dispatch, verify it, and then
enable the schedule again.

A chart or script that needs Kubernetes access beyond `ops/rbac/deployer.yaml`
fails `scripts/demo/deployer_access_test.py` in the pull request, so the role
changes in the same pull request. A merge does not change the cluster: an
administrator must apply the changed role and rerun `check` before the next run
([Changing the role](deployer-access.md#changing-the-role)). Until then
**Verify the deployment identity's access** stops each run before any change.
Merge such a change after the 13:00 UTC run, or pause the schedule, until the
role is applied.

### When a change alters a release's objects

A chart change that adds, removes or renames an object, or alters a hook or an
RBAC object, no longer needs a manual upgrade before the schedule accepts it. The
next scheduled run upgrades the release in place like any other change. Before
merging, `python3 scripts/demo/chart_objects.py --check` names the objects a chart adds
and the reviewed records it no longer renders, and the role must cover them.

To apply such a change under review rather than unattended, pause the schedule
first, then:

1. Dispatch **Deploy Jaeger Demo to OKE** from `main` (the workflow always runs
   `upgrade`).
   - **Scripts engine only**, before Helmfile adoption: dispatch for the affected
     component. The OTel sample application is `demo_stack=otel` and
     `otel_deploy_scope=otel-demo`. OCI monitoring is `demo_stack=oci` and
     `otel_deploy_scope=monitoring`.
   - **After Helmfile adoption**: the Helmfile engine has no component scope and
     ignores those two inputs. Dispatch an apply of all ten releases with the
     reviewed commit; helm-diff changes only the affected releases:

     ```bash
     gh workflow run ci-deploy-demo.yml --repo jaegertracing/jaeger-demo --ref main \
       -f engine=helmfile -f expected_config_sha="$(git rev-parse origin/main)"
     ```
2. Verify the component as in step 5 of the
   [change and verification process](#change-and-verification-process). For the
   OTel sample application, confirm that <https://shop.demo.jaegertracing.io/>
   loads, that <https://jaeger.demo.jaegertracing.io> shows a fresh `checkout`
   trace, and that `python3 scripts/demo/stack_health.py --stack otel` passes.
3. Enable the schedule again and confirm that the next scheduled run succeeds.
   The change is not accepted until it does.

Both paths need an identity with access to the affected release.
[#87](https://github.com/jaegertracing/jaeger-demo/issues/87) records that the
`demo-apply` grants from the script handover covered the two Jaeger releases only,
and that a monitoring upgrade through the workflow failed part-way.
[Deployer access](deployer-access.md) defines the reviewed role for all six
releases. Dispatch step 1 for those components, or let a scheduled run apply
the change, only after an administrator has applied that role and its `check`
command reports no difference.
