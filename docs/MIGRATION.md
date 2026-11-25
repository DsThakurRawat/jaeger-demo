<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Hosted-demo migration plan

Updated November 24, 2026. Both manual production handovers succeeded. Maintainers selected all five components for the daily refresh and first ran it through an in-cluster executor. That executor completed one scheduled run, on November 20, and failed to prepare the next two. The schedule switch was off from November 23 at 00:15 UTC to 03:00 UTC. [#95](https://github.com/jaegertracing/jaeger-demo/issues/95) replaces the executor with direct deployment: the scheduled run and a manual run are the same workflow job, which runs the deployment scripts under the reviewed [deployer roles](deployer-access.md). On November 23 the role was applied, the manual identity was bound, all five components were upgraded manually through that path, and on the maintainer's go-ahead the scheduled identity was bound and the schedule switch was turned on at 03:00 UTC. The operator recorded acceptance of the manual runs at 11:25 UTC, after that binding. The first scheduled run through the path, for the 13:00 UTC slot on November 23, succeeded and was accepted. After that acceptance the executor objects were removed from the cluster the same day: its admission policies and bindings, its RBAC, its namespace and its CRD ([executor retirement](executor-retirement.md#remove-the-executor-objects) steps 11 to 16). The leftovers that step 16 lists for separate approval were retired that evening: the Stage 1 handover grants, the `demo-plan` environment with its OCI identity, and the published executor images. The first scheduled run after these removals, for the 13:00 UTC slot on November 24, succeeded. [#107](https://github.com/jaegertracing/jaeger-demo/pull/107) had removed the executor code, workflows and documents from the repository earlier on November 23, before the cluster removal; the removal steps use only `kubectl` and did not depend on that code. See [current evidence](#current-evidence-and-unresolved-gates). [Executor retirement](executor-retirement.md) · [Deployer access](deployer-access.md) · [Releases](releases.md)

## Ownership and fixed constraints

AI drives implementation, testing, PR preparation and execution within the accepted scope. Human verification applies to the access plan, first script handover, first Helmfile adoption and final acceptance. Each review-feedback reply requires individual human approval under the organization AI Usage Policy.

Core retains product source, image publishing, ordinary examples and integration tests. The demo repository owns hosted configuration and deployment releases.

- Existing OKE cluster, public URLs, DNS, namespaces, PVCs and six application release names
- Certificate issuers externally managed
- No ingress-controller/cert-manager upgrade or AI activation
- Generated traces are disposable. Destructive storage resets require explicit approval.
- Generated code and submodules untouched

The source-only [ingress-controller dependency inventory](ingress-controller-dependencies.md) tracks #41; it authorizes no migration. The controller's owner is the cluster administrator (`jkowall`). A read-only check of the cluster on November 23 found the community [ingress-nginx](https://github.com/kubernetes/ingress-nginx) controller, installed from its static manifest rather than a Helm release. Upstream ended its maintenance in March 2026, so it receives no further releases or security fixes ([Ingress NGINX retirement](https://kubernetes.io/blog/2025/11/11/ingress-nginx-retirement/)). The exact installed version is kept out of this public repository. No replacement is selected, and its timing relative to the Helmfile phases is still undecided.

## Execution contract

- Implementation stages start only after predecessor exits are met. The core HotROD fixture runs in parallel with extraction. Helmfile conversion waits for script handover and the fixture to merge; post-adoption refactors merge before core removal.
- A deployment is the reviewed configuration at one commit on main, applied by the deploy workflow from GitHub Actions. This replaces the earlier plan-and-apply contract, in which a plan produced an immutable bundle and a separate apply consumed it ([#95](https://github.com/jaegertracing/jaeger-demo/issues/95)). No generated plan, bundle or in-cluster runner sits between review and apply.
- The workflow resolves the Jaeger and HotROD snapshot tags at deploy time, installs public charts only from archives that match the committed SHA-256 lock, checks its identity's access before any change, deploys components independently, and records each successful component's fingerprint. A scheduled run upgrades existing releases in place and never installs, creates a namespace or resets storage.
- The control on what reaches the cluster is code-owner review and merge to main, the main-only GitHub environments, and reviewer approval of each manual run. Scheduled runs have no reviewer. The Kubernetes access of both identities is one committed role; it limits mistakes and is not a boundary against someone who controls an identity ([deployer access](deployer-access.md)).
- Workflows that change the cluster share one concurrency group, so one runs at a time. There is no lock in the cluster.
- Helmfile, when adopted, replaces the scripts inside this model. It does not reintroduce a bundle or a second identity.
- First ownership adoption freezes live chart, values/config and exact image references. Preserve supplemental UIDs/selectors/Service ClusterIPs/pod-template hashes. Do not silently convert existing application image tags to digest syntax during adoption. Verify no-op behavior, then promote digest-pinned candidate images in a distinct reviewed deployment.
- Merge the extraction preserving both imported authored history and seed-repository history. Do not squash or rebase away imported commits. Verify filtered source ancestors are reachable from main after merge.
- Existing functional defects block the first script handover and Helmfile adoption, not local implementation. Refresh a healthy baseline immediately before each production transition.
- Helmfile automatic apply remains disabled until the adoption render is reviewed and manual adoption is verified.
- Respect required checks and maintainer review; never bypass branch protections.
- Do not expose plaintext credentials. Review new access/identity changes before creating or broadening permissions.

## Phases

### 1. Foundation

Status: repository controls verified; Renovate app coverage remains unresolved.

Deliverables:

- Public Apache-2.0 repository and five prerequisite issues: completed
- Bootstrap CODEOWNERS, credential-free CI, contributor/agent and AI-policy guidance delivered in [#3](https://github.com/jaegertracing/jaeger-demo/pull/3)
- DCO app, required PR review/checks, maintainer ownership and read-only Actions defaults verified; Renovate app coverage still needs verification
- Version-control this migration plan with status, issue/PR links, decisions, and evidence

Exit criteria:

- [x] Bootstrap checks exist and pass; required review/check settings are verified and restored after the approved one-time repository review bypass
- [ ] App installation/access gaps have explicit owners; deployment workflows remain manual-only and production access is not enabled prematurely

### 2. Extraction

Status: merged in [#3](https://github.com/jaegertracing/jaeger-demo/pull/3). Tool pins, the restricted handover guards and the deployed OCI configuration are recorded in [#5](https://github.com/jaegertracing/jaeger-demo/pull/5), [#7](https://github.com/jaegertracing/jaeger-demo/pull/7) and [#9](https://github.com/jaegertracing/jaeger-demo/pull/9).

Issues: [jaeger#10189](https://github.com/jaegertracing/jaeger/issues/10189).

Deliverables:

- History-preserving extraction at a freshly resolved upstream main cutoff, including scripts/demo/** and all test dependencies
- Minimal documented adapters for upstream image SHAs, paths, environments and hosted-only Renovate rules
- Credential-free shell/Python/Renovate CI; production workflows main-only and manual-only
- README architecture/ownership, cutoff and pending-change list; contribution, access, script-release, rollback and troubleshooting documentation

Exit criteria:

- [x] Local tests and PR CI passed; the user-authorized history-preserving merge is [613f016](https://github.com/jaegertracing/jaeger-demo/commit/613f01691a2a24704f6de00b201149a0975b03d1). The temporary `jkowall` review bypass was removed immediately afterward and original protections were verified restored.
- [x] All 55 mapped imported authored commits and seed history are reachable from target main; source cutoff and adapter audit recorded
- [ ] Documentation commands validated; deployment-ready configuration commit pinned; no hosted core assets removed

### 3. Core hotrod fixture

The core-owned fixture merged at `e9252b8` on November 18, 2026. Both Docker and Kubernetes HotROD jobs passed. [Core fixture PR](https://github.com/jaegertracing/jaeger/pull/10194)

Issues: [jaeger#10190](https://github.com/jaegertracing/jaeger/issues/10190).

Deliverables:

- Minimal core-owned memory Jaeger/HotROD fixture using localhost:5000 images and pullPolicy Never
- Independent config.yaml and ui-config.json test inputs, regression tests, local invocation/cleanup/troubleshooting documentation
- Keep product image publishing and Docker test behavior intact

Exit criteria:

- [x] Core make fmt, make lint, make test and appropriate patch coverage pass
- [x] Docker and Kubernetes HotROD CI passed with the core-owned test fixture and the reviewed PR merged to core main

### 4. Verbatim script handover

Status: the [OCI run](https://github.com/jaegertracing/jaeger-demo/actions/runs/44380168431) reached revision 25 and the [OTel run](https://github.com/jaegertracing/jaeger-demo/actions/runs/44383352094) reached revision 14 at configuration `df261c2`. Both demos produced fresh queryable traces. The scheduled scope includes all five components and uses the existing live OpenSearch volume without snapshot or restore. Manual backup and recovery remained available during Stage 1; [#76](https://github.com/jaegertracing/jaeger-demo/issues/76) removes those tools from the repository as part of phase 5. See the [script schedule](https://github.com/jaegertracing/jaeger-demo/issues/11) for access, rehearsal and first-run verification. The executor-based schedule completed its first run on November 20 and failed on November 21 and 22. It was paused on November 23, and the direct path in [#95](https://github.com/jaegertracing/jaeger-demo/issues/95) replaced it the same day; see [current evidence](#current-evidence-and-unresolved-gates). The remaining exit criterion is met by the manual upgrades and the first accepted scheduled run in [executor retirement](executor-retirement.md).

Requires: foundation, extraction.

Deliverables:

- Refresh healthy functional baseline for both demos; separately tracked failures must be resolved
- Prepare immutable handover inputs, resource/route/storage diff and precise rollback procedure
- Prepare access plan; after human verification, provision fresh new-repo OCI access and separate plan/apply identities, verify permissions without deployment writes
- Human verifies handover bundle; disable all three old workflows and drain running AND pending runs
- Retire core delivery of the signing key after draining, then deploy the verified bundle through the new manual path
- Verify both stacks, then enable the script schedule and verify one scheduled refresh

Exit criteria:

- [x] Healthy baseline verified before the first production change and recorded with the reviewed configuration and rollback captures
- [x] Human-verified handover used merged main and frozen inputs. All three old workflows were disabled and drained. Core’s Actions signing secret was deleted. The shared maintainer OCI key remains active.
- [x] Manual deployment and scheduled refresh succeed without unexplained resource, route or storage changes
- [x] The manual handover records contain the actual revisions, verification and per-stack rollback inputs

### 5. Helmfile conversion

Credential-free implementation and rehearsal are in progress. The plan-and-apply bundle this phase was first written around is retired by [#95](https://github.com/jaegertracing/jaeger-demo/issues/95), together with bundle custody ([#71](https://github.com/jaegertracing/jaeger-demo/issues/71), [#72](https://github.com/jaegertracing/jaeger-demo/issues/72), [#73](https://github.com/jaegertracing/jaeger-demo/issues/73)) and the runtime decision in [#69](https://github.com/jaegertracing/jaeger-demo/issues/69).

Issues: [jaeger-demo#1](https://github.com/jaegertracing/jaeger-demo/issues/1).

The candidate configuration renders all six application releases and four namespace-local companion releases. `make validate` compares the application manifests against the extracted scripts and schema-checks both sets. The ownership rehearsal runs on a full Kubernetes node in PR CI. The release phase rehearsal covers healthy no-op applies, retry after a failed release, and pending-state blocking. It also runs native Helmfile with the direct deployment flags (`--install=false`, `--reset-values`, values passed to Helm) and covers a Secret-only change, an empty-diff failed release under `apply`, the history limit and refusal to install a missing release. The companion rehearsal's first install is a plain `helm install` without `--take-ownership`. [#99](https://github.com/jaegertracing/jaeger-demo/issues/99) removed the frozen-bundle library, its rehearsals, the `verify-frozen-manifests` post-renderer and the contract document. Kept for adoption: snapshot image resolution and the Jaeger configuration gate (`promotion_images.py`), failed and pending release handling with OCI before OTel (`release_phases.py`), the read-only live object reader (`live_observation.py`) and the companion ownership patches. Running `helmfile apply` from the deploy workflow with these pieces is not implemented. Render-time chart archive verification now exists in the repository: `make validate` renders only from `.helmfile-locked.yaml`, whose charts are archives verified against `scripts/demo/chart-archives.lock.json` ([#140](https://github.com/jaegertracing/jaeger-demo/issues/140)). Applying only verified archives is still [#73](https://github.com/jaegertracing/jaeger-demo/issues/73) and is not done. The complete live-equivalence inventory ([#70](https://github.com/jaegertracing/jaeger-demo/issues/70)) and production adoption are outstanding. [#76](https://github.com/jaegertracing/jaeger-demo/issues/76) removes the backup and restore tooling, the recovery workflow and the `opensearch_recovery` gate from the repository ahead of the Helmfile cutover, keeps volume expansion, and reduces the `jaeger-demo-recovery` role in `ops/rbac/deployer.yaml` to what expansion needs. The constrained manual reset of that deliverable is the **Reset OTel Demo OpenSearch Trace Data** workflow: after a typed acknowledgment and reviewer approval it deletes the daily Jaeger trace indices through the OpenSearch API, restarts Jaeger and verifies a fresh trace, without deleting a Kubernetes object ([reset the trace data](releases.md#reset-the-trace-data)). It is unit-tested and has a rehearsal in the PR CI kind job; it has no production run, and it does not replace the data volume. Applying the reduced role and removing what the tooling left in the cluster are operator steps ([OpenSearch storage maintenance](releases.md#opensearch-storage-maintenance)); neither is recorded here as done. [Application releases](helmfile-applications.md) · [Companion rehearsal](helmfile-companions.md)

Requires: verbatim-script-handover, core-hotrod-fixture, and an accepted direct scheduled run.

Deliverables:

- Pinned/checksummed toolchain, six labeled application releases and allowlisted namespace-local companion resources
- `helmfile apply` from the deploy workflow in place of the scripts: image resolution at deploy time, chart archive verification (render-time verification exists; verifying at apply is #73 and not done), compatibility validation before any mutation, failed/pending state handling, OCI-before-OTel verification and checks even on empty diffs
- Complete sanitized equivalence fixture, live drift report, deployer role coverage of the Helmfile render, and kind regression/adoption/rollback tests
- Delete backup/restore tooling and recovery gate; retain volume expansion and explicit constrained manual reset. Done in the repository by [#131](https://github.com/jaegertracing/jaeger-demo/pull/131) ([#76](https://github.com/jaegertracing/jaeger-demo/issues/76)). The reset has no production run; applying the reduced `jaeger-demo-recovery` role and removing what the tooling left in the cluster remain [operator steps](releases.md#opensearch-storage-maintenance).
- Native Helmfile Renovate, daily checks, seven-day release age, reviewed updates without automerge
- Architecture, configuration/selectors, access/RBAC, release/promotion/rollback, maintenance/reset/incidents and troubleshooting runbooks

Exit criteria:

- [ ] All local/kind/schema/render/equivalence checks pass, including unchanged adoption identities and pod templates
- [ ] Last release-writer Helm major established; digest support confirmed; preliminary inventory gaps resolved
- [ ] Runbook commands validated and adoption/rollback rehearsed; reviewed PR merged to main
- [ ] Automatic apply remains disabled; manual adoption configuration commit pinned

### 6. Manual helmfile adoption

Status: pending.

Requires: helmfile-conversion.

Deliverables:

- Refresh healthy baseline, complete inventory and relevant live drift report
- Human verifies the adoption render at its commit, the adoption allowlist, exact live references and rollback revisions
- Adopt only allowlisted supplemental resources with keep policy; first companion install without atomic; preserve live application inputs
- Verify OCI before OTel mutation and all functional/resource invariants; perform a second no-op apply

Exit criteria:

- [ ] Initial manual adoption and no-op apply verified with unchanged supplemental UIDs/selectors/ClusterIPs/pod-template hashes
- [ ] No unexplained application rollout or resource/storage/route drift
- [ ] Human verification of successful adoption unlocks scheduled Helmfile operations

### 7. Promotion and acceptance

Status: pending.

Requires: manual-helmfile-adoption.

Deliverables:

- Switch the schedule from the scripts to Helmfile only after manual adoption verification
- Promote digest-pinned candidate images separately from ownership adoption, each as a reviewed commit
- Verify one scheduled refresh and one reviewed Renovate chart bump through the new path
- Observe at least 24 hours; maintain fresh functional evidence for traces, traffic, UI, finite SPM, Grafana, TLS and analytics
- Record configuration/image/chart/tool provenance, rollback revisions and acceptance evidence; human verifies final acceptance

Exit criteria:

- [ ] No-op, scheduled refresh and reviewed chart bump successful; minimum 24-hour observation complete
- [ ] Documentation/release-process rehearsal and final healthy evidence complete
- [ ] Human accepts cutover before post-adoption refactors start

### 8. Post adoption refactors

Status: pending.

Issues: [jaeger-demo#2](https://github.com/jaegertracing/jaeger-demo/issues/2).

Requires: promotion-and-acceptance.

Deliverables:

- Separate scoped issues/PRs for shared generator/UI, checksum restarts, removal of routine HotROD deletion and fingerprint reconciler
- Update affected architecture/configuration/release/runbook documentation with each behavior change
- Retain legacy revision ConfigMap until core removal and separate retirement

Exit criteria:

- [ ] All scoped refactors reviewed, tested and merged; functional/no-unnecessary-restart behavior verified
- [ ] Legacy revision state remains available; obsolete runbook instructions removed

### 9. Core removal

Status: pending.

Issues: [jaeger#10191](https://github.com/jaegertracing/jaeger/issues/10191).

Requires: core-hotrod-fixture, post-adoption-refactors.

Deliverables:

- Remove hosted assets/workflows/tests/Renovate rules after references are independent
- Update workflow index, HotROD README, release checklist, orchestrator references and example-path stub READMEs
- Verify final ownership split: core publishes products; jaeger-demo owns hosted deployment releases
- Retire legacy revision ConfigMap only after verified removal and scoped human verification

Exit criteria:

- [ ] Whole-core make fmt, make lint, make test and HotROD CI pass; reviewed removal PR merged
- [ ] Core build/test/release instructions do not depend on removed hosted paths or the new repository
- [ ] Final live health, documentation, workflow/credential retirement and state-retirement evidence recorded

## Release and documentation contract

- A hosted deployment is identified by its configuration commit plus chart/tool pins, independent upstream image commits/digests, exact image references and verification record. Core remains responsible for Jaeger/HotROD image publishing and product releases.
- Workflow summaries and documented durable records include the configuration commit, selected/changed releases, checks and outcomes, prior Helm revisions and rollback inputs. Define retention before operational handover.
- Docs ship with implementation, tested examples and stage exit evidence. Cover architecture, prerequisites, contributor/agent workflow, configuration, access, releases/promotion, maintenance/reset, rollback and incidents.
- Reviewed configuration/chart changes and accepted scheduled image refreshes run under standing authorization after the initial gates. A changed access scope, destructive reset or departure from documented invariants requires a concrete decision.

## Current evidence and unresolved gates

- Extraction source cutoff: [bcef01d0c848d5be0416c2eee117d5d306416dd7](https://github.com/jaegertracing/jaeger/commit/bcef01d0c848d5be0416c2eee117d5d306416dd7). Independent verification confirmed all 51 imported file blobs match that source. After [#3](https://github.com/jaegertracing/jaeger-demo/pull/3) merged on November 17 as `613f016`, all 55 mapped authored commits and the seed remained reachable from main. [Main-branch Demo CI](https://github.com/jaegertracing/jaeger-demo/actions/runs/44193888416) passed. See the README for pending changes to rehome.
- A preliminary sanitized live inventory was collected November 15, 2026. It omits embedded configuration and is not the full equivalence fixture.
- Post-handover checks found fresh traces on both demos and finite checkout SPM during observed checkout traffic. Refresh the baseline before schedule activation and later Helmfile adoption.
- The historical [November 17 core writer](https://github.com/jaegertracing/jaeger/actions/runs/44090222704/job/134063110067) used Helm v4.3.2 and OCI CLI 3.96.3; its `default/jaeger` upgrade succeeded at revision 22 despite an earlier recovery failure blocking OTel components. The November 18 manual handover runs above supersede that revision. Compare live revisions with writer evidence before each transition.
- DCO app and required repository controls are verified. The historical-import exception passed at `e169d72`; the approved temporary review bypass used for #3 was removed. Renovate configuration/discovery CI passes, but the app's repository access is still unverified.
- Daily schedule, first run: `DEMO_SCHEDULE_ENABLED` was set to `true` on November 20 at 12:44 UTC. [Run 44742195163](https://github.com/jaegertracing/jaeger-demo/actions/runs/44742195163) signalled the 13:00 UTC slot at configuration `1819e9a` and reported "Protected executor verified bundle 406fe1a80c6098002af4fe9ee7f9cd3993f1630205cb24225433c185fd2018e5" at 13:14:57 UTC. The executor image for that commit was [published](https://github.com/jaegertracing/jaeger-demo/actions/runs/44619647169) on November 19. Operator acceptance of this run, which reconciles the six releases and five component fingerprints and verifies fresh traces, public health, TLS and storage bindings ([commissioning and first-run criteria](https://github.com/jaegertracing/jaeger-demo/blob/f9b54fe60b039633d00b5ea91dba6a40722e8fad/docs/daily-production-install-review.md)), is not recorded yet.
- Daily schedule, November 21: [run 44907484823](https://github.com/jaegertracing/jaeger-demo/actions/runs/44907484823) at configuration `ebbb98b` failed at 13:10:41 UTC with "Invalid reviewed execution engine or schedule interval". No preparation had been published for the slot. An operator read the cluster the same day, without changing it: the 12:20 UTC preparer Job started at 12:20:00 and failed at 13:00:00 UTC with `DeadlineExceeded`, its 40-minute limit; the node that runs the executor held 31 running pods against a limit of 31, the newest two created by the November 20 run; and the preparer has a required affinity to that node, because both mount the same single-attach volume. The request record was still the November 20 slot in state `succeeded`, so the requester did not signal, and the checkpoint named no lock holder. The scheduler's events had expired, so the reason the pod stayed unscheduled is inferred from the full node, not read. A refusal over guarded files exits within a minute and would not reach the deadline. Separately, the installed executor image was still the one for `1819e9a`, while merges after it had changed four executor-guarded files (both `deploy-all.sh`, `deploy.py` and `verify_trace.py`), so that preparation would have been refused had it started. The image [published](https://github.com/jaegertracing/jaeger-demo/actions/runs/44849897776) for `a06fb5d` was not installed, and later merges changed further guarded files. The next slot needs a free pod slot on the executor's node and an installed image that matches `main` ([guarded files](https://github.com/jaegertracing/jaeger-demo/blob/f9b54fe60b039633d00b5ea91dba6a40722e8fad/docs/releases.md#merging-a-change-to-guarded-files), [a preparation that did not run](https://github.com/jaegertracing/jaeger-demo/blob/f9b54fe60b039633d00b5ea91dba6a40722e8fad/docs/releases.md#failure-handling)). [#81](https://github.com/jaegertracing/jaeger-demo/issues/81) tracks the misleading message and the late report.
- Daily schedule, November 22: [run 45026014303](https://github.com/jaegertracing/jaeger-demo/actions/runs/45026014303) failed with no preparation published for the slot. [#94](https://github.com/jaegertracing/jaeger-demo/issues/94) records the cause read from the cluster: after [#56](https://github.com/jaegertracing/jaeger-demo/pull/56) changed the object inventory, the executor image was reinstalled but its permissions and admission guards were not, so the preparer could not read the new OTel application objects.
- Schedule paused: `DEMO_SCHEDULE_ENABLED` was `false` from November 23 at 00:15 UTC until step 9 below. Read from the cluster on November 23 shortly after 02:02 UTC ([executor retirement](executor-retirement.md#before-the-first-direct-run) steps 2 to 5): the `jaeger-demo-preparer` CronJob was suspended with no active Job, `jaeger-demo-executor` was at 0 replicas, the `daily-hosted-update` request was in state `succeeded` for the November 20 slot, and `default/jaeger-demo-deployment-revisions` carried no `jaegertracing.io/deployment-lock-holder` annotation, so step 5 was not needed. The executor namespace, admission policies and CRD were still installed at that read; steps 11 to 16 below removed them.
- Direct deployment ([#95](https://github.com/jaegertracing/jaeger-demo/issues/95), Option A): chart archive verification ([#96](https://github.com/jaegertracing/jaeger-demo/pull/96)), the deployer role ([#97](https://github.com/jaegertracing/jaeger-demo/pull/97)) and the workflow change ([#101](https://github.com/jaegertracing/jaeger-demo/pull/101)) define the path.
  - Step 6, November 23: `ops/rbac/deployer.yaml` at `edc86d9` was applied at 02:02:33 UTC and bound to the manual (`demo-apply`) identity only. `deployer_access.py check --identity manual` reported `0 difference(s)`.
  - Step 7, November 23: one manual run per component, in the runbook's order, each approved by a maintainer in `demo-apply`. Every run first passed the workflow's own access check.
    1. `oci` / `monitoring`, the upgrade that failed in [#87](https://github.com/jaegertracing/jaeger-demo/issues/87): [run 45098046962](https://github.com/jaegertracing/jaeger-demo/actions/runs/45098046962) at `3108e0c`, `prometheus` revision 9.
    2. `oci` / `jaeger`: [run 45098644556](https://github.com/jaegertracing/jaeger-demo/actions/runs/45098644556) at `1708976`, `default/jaeger` revision 27. `image_tag` was pinned to the running snapshot `22e560d25d0c976199dd845296fa0259bff3cf55` because upstream `main` had no published snapshot; the unpinned [run 45098404066](https://github.com/jaegertracing/jaeger-demo/actions/runs/45098404066) stopped before any cluster change ([#110](https://github.com/jaegertracing/jaeger-demo/issues/110)).
    3. `otel` / `opensearch` with `opensearch_recovery=waive`: [run 45099717009](https://github.com/jaegertracing/jaeger-demo/actions/runs/45099717009) at `6325eb3`, `opensearch` and `opensearch-dashboards` revision 19, data PVC unchanged. The earlier [run 45098933253](https://github.com/jaegertracing/jaeger-demo/actions/runs/45098933253) completed the rollout but failed verification on a Dashboards 503 just after Helm reported ready ([#111](https://github.com/jaegertracing/jaeger-demo/issues/111)), so it recorded no fingerprint.
    4. `otel` / `jaeger`: [run 45100227684](https://github.com/jaegertracing/jaeger-demo/actions/runs/45100227684) at `6325eb3`, `jaeger/jaeger` revision 16, `image_tag` pinned as in 2.
    5. `otel` / `otel-demo`: [run 45100614818](https://github.com/jaegertracing/jaeger-demo/actions/runs/45100614818) at `d27cb23`, `otel-demo` revision 16.
  - After step 7, at 02:33 UTC: all six releases were `deployed`, the five fingerprints in `default/jaeger-demo-deployment-revisions` matched `d27cb23`, the public demo, Jaeger UI and Dashboards answered, and Dashboards landed in its Observability workspace.
  - Acceptance of step 7, November 23: the operator (`jkowall`) accepted the five manual runs at 11:25 UTC, in the [comment that closed #87](https://github.com/jaegertracing/jaeger-demo/issues/87#issuecomment-7099625650).
  - Node disk, November 23: the node's root filesystem used only 29.4 GB of its 100 GB boot volume. It reached disk pressure from 02:12:26 to 02:18:04 UTC, during these runs, and kubelet evicted the OCI Prometheus Pod and trace generator; both were rescheduled. The filesystem was then grown online with `oci-growfs` to 82.8 GB, with about 58 GB free. That fix lives on this instance only: read at 12:59 UTC, the node pool set no initialization script, so a replacement node would boot with the default-sized root partition. [#120](https://github.com/jaegertracing/jaeger-demo/issues/120) adds the script and the operator steps ([node pool](node-pool.md)); setting it on the pool is a separately approved operator action and is not recorded here.
  - Monitoring chart 93.6.1, November 23: [#59](https://github.com/jaegertracing/jaeger-demo/pull/59) merged at 02:36 UTC as `5bf8cfc`. [Run 45101335987](https://github.com/jaegertracing/jaeger-demo/actions/runs/45101335987) (`oci` / `monitoring`, approved in `demo-apply`) upgraded `prometheus` to revision 10, `kube-prometheus-stack-93.6.1`. Read at 02:45 UTC: every monitoring Pod `Running`, the `prometheus-grafana` Role and RoleBinding removed, all 15 Prometheus targets up (including the API server and three kubelet targets through the control-plane scrape class), only `Watchdog` and `InfoInhibitor` firing, and the public Grafana health endpoint reporting 13.3.1 with database `ok`.
  - Step 8, November 23, on the maintainer's go-ahead after step 7: the bindings from `a03ba66` were applied for both identities. The six `jaeger-demo-deployer` bindings name both users; the recovery bindings name the manual user only. `check --identity scheduled` first reported one difference: the scheduled user could list PersistentVolumeClaims in `opensearch` through its earlier read-only `jaeger-demo-scheduled-observer` binding. Its four `jaeger-demo-scheduled-observer` RoleBindings and the `jaeger-demo-scheduled-namespaces` ClusterRoleBinding were exported outside Git and deleted. At 02:59 UTC `check` reported `0 difference(s)` for both identities. The live role is still the one applied at `edc86d9`; it keeps the `bind` and `escalate` grant on the `prometheus-grafana` Role that [#59](https://github.com/jaegertracing/jaeger-demo/pull/59) removed from the file. The GitHub settings the step names were read: `demo-scheduled` allows `main` only and has no reviewer, the OCI secrets exist only as environment secrets, branch protection on `main` requires code-owner review of the latest push, and `renovate.json` sets `automerge: false`. The Renovate app's own settings were not visible.
  - Step 9, November 23: the [scheduled scope review](cutover.md#review-scheduled-scope) at `725097e` selected `otel-jaeger` and `oci-jaeger` only; all five stored fingerprints matched. With no deploy run queued and no organization variable of the same name, `DEMO_SCHEDULE_ENABLED` was set to `true` at 03:00 UTC.
  - Step 10, November 23: [run 45181733526](https://github.com/jaegertracing/jaeger-demo/actions/runs/45181733526), the first scheduled run through the direct path, ran from 13:13 to 13:16 UTC at `1d2ff80` with no reviewer, passed its access check, and deployed `otel-jaeger`, `otel-demo` and `oci-jaeger`. `otel-demo` was selected because [#118](https://github.com/jaegertracing/jaeger-demo/pull/118), merged after the scope review, changed `examples/otel-demo/otel-demo-values.yaml`. Read at 13:17 UTC: all six releases `deployed` (`jaeger/jaeger` 17, `default/jaeger` 28 and `otel-demo/otel-demo` 17 from this run; `opensearch` and `opensearch-dashboards` 19; `prometheus` 10); fingerprints `oci-jaeger` `2e9a3de1`, `oci-monitoring` `16c3b62e`, `opensearch` `3029894f`, `otel-demo` `0eaa3aaf`, `otel-jaeger` `4a9ac86a`; the OpenSearch data PVC `Bound` to its existing volume; all four Certificates `Ready`; HotROD, Jaeger, the shop and Grafana answering with their public certificates valid; spans from the last ten minutes in both Jaeger instances. The operator (`jkowall`) accepted the run the same day.
  - Removal of the executor objects, November 23, by the cluster administrator (`jkowall`), who approved each step separately after seeing what it would delete. No deploy, recovery or volume expansion run was in progress before any step.
    - Before step 11, read at 13:43 to 13:47 UTC: the preparer CronJob suspended, the executor Deployment at 0 replicas, the request record in state `succeeded` for the November 20 slot, 12 `DailyWorkloadApproval` objects, 167 policies and 193 bindings under the executor's two name prefixes, and 36 bindings naming service accounts in `jaeger-demo-operations`.
    - Step 11: full manifests were exported between 14:35:33 and 14:35:49 UTC to private custody outside Git: every admission policy and binding (169 and 195), the 39 objects in the executor namespace, every ClusterRole and ClusterRoleBinding (118 and 100), every Role and RoleBinding (60 and 65), and the CRD, the PersistentVolumes, the namespace object and `default/jaeger-demo-deployment-revisions`. Each file parsed; sizes and SHA-256 hashes are in the release record. The operator decided that the outcome records on the `jaeger-demo-bundles` volume were not to be kept.
    - Step 12: the lists read at 14:50 UTC held 193 bindings and 167 policies. The only other admission policies in the cluster, OKE's `npn-pod-deletion-deny-policy` and `oke-application-resource-request` with one binding each, were left alone. The bindings were deleted from 14:57:18 to 14:58:35 UTC and the policies from 14:59:47 to 15:00:53 UTC. Both listings were then empty, and at 15:01:08 UTC the server-side dry-run patch of `default/jaeger-demo-deployment-revisions` was admitted.
    - Step 13: 68 objects were deleted by exact name between 18:45 and 18:47 UTC, after the list was read again and found unchanged: the ClusterRoles and ClusterRoleBindings `jaeger-demo-existing-workload`, `jaeger-demo-monitoring-hook` and `jaeger-demo-workload-observe`, and 31 Roles with their 31 RoleBindings in `default`, `jaeger`, `opensearch`, `otel-demo` and `kube-system`. Every binding named only service accounts in the executor namespace, and no other binding used those roles. The live list held one name the runbook's table does not: Role and RoleBinding `default/jaeger-demo-recover-daily-checkpoint`, bound to the executor's `jaeger-demo-recovery` service account. Read before the delete, it granted only `get` and `patch` on the `jaeger-demo-deployment-revisions` ConfigMap, so it was the recovery operator's grant that step 16 lists, and the operator approved removing it here by name. `jaeger-demo-guarded-existing` was not installed. Afterwards the only bindings naming executor service accounts were two inside the executor namespace, the `jaeger-demo-deployer` and `jaeger-demo-recovery` roles were present, and at 18:53 UTC `check` at `1d2ff80` reported `0 difference(s)` for both identities.
    - Step 14: the bundle volume's reclaim policy was `Delete`, and the request record's final state was `succeeded` for the November 20 slot. `kubectl delete namespace jaeger-demo-operations` ran from 18:59:16 to 18:59:28 UTC. The namespace was gone, not `Terminating`, and the 50Gi PersistentVolume was deleted with its claim.
    - Step 15: with no `DailyWorkloadApproval` object left, the CRD `dailyworkloadapprovals.demo.jaegertracing.io` was deleted at 19:00:54 UTC.
    - Step 16: `jaegertracing.io/deployment-lock-version` (value `v1`), the only lock annotation on `default/jaeger-demo-deployment-revisions`, was removed at 19:02:28 UTC against the resource version just read. The ConfigMap's UID and its five fingerprints were unchanged.
    - After step 16, read at 19:02 to 19:03 UTC: all six releases `deployed` at the revisions of step 10, both `check` commands reporting `0 difference(s)`, no Pod outside `Running` or `Completed`, the OpenSearch data PVC `Bound`, and the Jaeger UI, the shop, HotROD and the Dashboards status endpoint answering 200 with valid certificates.
  - Step 16 leftovers, November 23 ([step 16](executor-retirement.md#16-remove-the-lock-annotation-and-retire-leftovers)), retired by the cluster administrator (`jkowall`), who approved each one separately after seeing what it would remove. No deploy, recovery or volume expansion run was in progress. The recovery operator's grant on the deployment-revisions ConfigMap, which step 16 also lists, went in step 13, and the scheduled identity's earlier read-only bindings went in step 8.
    - Read first, from 21:51 to 21:59 UTC: no workflow on `main` named the `demo-plan` environment, and it had never recorded a deployment. Its identity was a third OCI user, separate from the manual and scheduled ones. That user's only cluster access was read-only: the `jaeger-demo-stage1-observer` RoleBindings in `default`, `jaeger`, `opensearch` and `otel-demo`, and one of the two subjects of the `jaeger-demo-stage1-namespaces` ClusterRoleBinding. The manual identity held the other `jaeger-demo-stage1-*` bindings and the scheduled identity held none. No binding outside that set used one of its roles, nothing on `main` outside the documents names those grants, and the eight live `jaeger-demo-deployer` and `jaeger-demo-recovery` roles had exactly the rules of `ops/rbac/deployer.yaml`, which is the same file at `1d2ff80` and `131029c`. The live `default` Role therefore no longer carried the `prometheus-grafana` grant that step 8 notes.
    - `demo-plan` environment: deleted at 22:46:27 UTC with its six OCI secrets and its branch policy. `demo-apply` and `demo-scheduled` kept their six secrets each.
    - Stage 1 handover grants: the 17 `jaeger-demo-stage1-*` objects were exported to private custody outside Git at 21:59 UTC, read again at 22:46 UTC and found unchanged, and deleted by exact name from 22:47:29 to 22:47:53 UTC. They were the ClusterRoles `jaeger-demo-stage1-namespaces` and `jaeger-demo-stage1-observer`, the ClusterRoleBinding `jaeger-demo-stage1-namespaces`, the Role `jaeger-demo-stage1-apply` in `default`, `jaeger`, `opensearch` and `otel-demo`, the Role `jaeger-demo-stage1-cluster-info` in `kube-system`, and nine RoleBindings: `jaeger-demo-stage1-apply` and `jaeger-demo-stage1-observer` in each of the four release namespaces and `jaeger-demo-stage1-cluster-info` in `kube-system`. `check` at `131029c` reported `0 difference(s)` for both identities at 22:47 UTC, before the delete, and at 22:48 UTC, after it. Afterwards the only bindings naming an OCI user were the six `jaeger-demo-deployer` bindings, for both users, and the two `jaeger-demo-recovery` bindings, for the manual user. Compared with the role file, the manual identity lost only reads that no script or workflow on `main` uses: `get` on the `default` namespace object, `get`, `list` and `watch` on endpoints and events in `default`, and in `jaeger` `watch` on endpoints, `get` and `watch` on events and `get`, `list` and `watch` on Jobs. The step 7 manual runs took place while that identity still held the Stage 1 grants, and the recovery test and volume expansion workflows have no run in this repository, so for those two paths `check` is the only evidence that the roles are enough.
    - Plan identity in OCI IAM, from 22:49 to 22:51 UTC: its API key was deleted, which revokes the credential the environment's secrets held. The policy statement that let its group use the cluster was removed, and the statement for the manual identity's group in the same policy was kept word for word. Its user and its group were then deleted.
    - Executor images: the operator decided not to keep them. The `ghcr.io/jaegertracing/jaeger-demo-executor` package, six images tagged with the commits `051ecdb`, `1819e9a`, `a06fb5d`, `8f21626`, `6ae9f76` and `f9b54fe`, was deleted at 22:53 UTC, and an anonymous pull was then refused. Their digests are in the release record, and the design they were built from stays readable at `f9b54fe`.
    - Also settled: the 50 GB block volume behind the bundle PersistentVolume that step 14 deleted was read as `TERMINATED` in OCI. A second, partial copy of the step 11 exports was deleted from private custody after five of its seven files matched the primary copy by hash and the other two were found to be subsets of it; the primary copy still verified against its recorded hashes afterwards.
    - After these removals, read from 22:51 to 22:53 UTC: all six releases `deployed` at the revisions of step 10, both `check` commands reporting `0 difference(s)`, no Pod outside `Running` or `Completed`, the OpenSearch data PVC `Bound`, and the Jaeger UI, the shop, HotROD and the Dashboards status endpoint answering 200 with valid certificates.
  - First scheduled run after the removals, November 24: [run 45401009102](https://github.com/jaegertracing/jaeger-demo/actions/runs/45401009102), for the 13:00 UTC slot, ran from 13:12 to 13:16 UTC at `131029c`. It was the first run after the executor objects and the step 16 leftovers were removed; run 45181733526 in step 10 finished before step 11 began. Its access check reported `0 difference(s)`. It selected `otel-jaeger` and `oci-jaeger` and upgraded `jaeger/jaeger` to revision 18 and `default/jaeger` to revision 29, and its log holds no refused request. Upstream Jaeger `main` had no published snapshot, so the run deployed the newest published one, the same tag as the day before. The Jaeger and HotROD Pods were therefore not replaced; only the trace generators restarted. Read at 13:36 UTC: both public Jaeger instances returned spans less than a minute old, and HotROD, the shop, the Dashboards status endpoint and Grafana answered 200 with valid certificates. Read from the cluster at 13:45 to 13:46 UTC: all six releases `deployed`, with `opensearch` and `opensearch-dashboards` at 19, `otel-demo` at 17 and `prometheus` at 10 as before; the five fingerprints unchanged from step 10; the OpenSearch data PVC `Bound`; all four Certificates `Ready`; no Pod outside `Running` or `Completed`; and `check` at `131029c` reporting `0 difference(s)` for both identities. The operator's acceptance of this run is not recorded here.
- Add delivering PR links, merged commits and evidence as phases complete. Do not mark planned work complete.
