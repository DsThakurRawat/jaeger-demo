<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Hosted-demo migration plan

Updated November 23, 2026. Both manual production handovers succeeded. Maintainers selected all five components for the daily refresh and first ran it through an in-cluster executor. That executor completed one scheduled run, on November 20, and failed to prepare the next two. The schedule switch has been off since November 23 at 00:15 UTC. [#95](https://github.com/jaegertracing/jaeger-demo/issues/95) replaces the executor with direct deployment: the scheduled run and a manual run are the same workflow job, which runs the deployment scripts under the reviewed [deployer roles](deployer-access.md). That path is defined in this repository and is not active in production until the role is applied, the identities are bound and the schedule switch is turned on. The executor objects stay in the cluster until the first direct scheduled run is accepted. See [current evidence](#current-evidence-and-unresolved-gates). [Executor retirement](executor-retirement.md) · [Deployer access](deployer-access.md) · [Releases](releases.md)

## Ownership and fixed constraints

AI drives implementation, testing, PR preparation and execution within the accepted scope. Human verification applies to the access plan, first script handover, first Helmfile adoption and final acceptance. Each review-feedback reply requires individual human approval under the organization AI Usage Policy.

Core retains product source, image publishing, ordinary examples and integration tests. The demo repository owns hosted configuration and deployment releases.

- Existing OKE cluster, public URLs, DNS, namespaces, PVCs and six application release names
- Certificate issuers externally managed
- No ingress-controller/cert-manager upgrade or AI activation
- Generated traces are disposable. Destructive storage resets require explicit approval.
- Generated code and submodules untouched

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

Status: the [OCI run](https://github.com/jaegertracing/jaeger-demo/actions/runs/44380168431) reached revision 25 and the [OTel run](https://github.com/jaegertracing/jaeger-demo/actions/runs/44383352094) reached revision 14 at configuration `df261c2`. Both demos produced fresh queryable traces. The scheduled scope includes all five components and uses the existing live OpenSearch volume without snapshot or restore. Manual backup and recovery remain available during Stage 1. The later Helmfile plan retains its agreed retirement of those tools. See the [script schedule](https://github.com/jaegertracing/jaeger-demo/issues/11) for access, rehearsal and first-run verification. The executor-based schedule completed its first run on November 20 and failed on November 21 and 22. It is paused, and the direct path in [#95](https://github.com/jaegertracing/jaeger-demo/issues/95) replaces it; see [current evidence](#current-evidence-and-unresolved-gates). The remaining exit criterion is met by the manual upgrades and the first accepted scheduled run in [executor retirement](executor-retirement.md).

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
- [ ] Manual deployment and scheduled refresh succeed without unexplained resource, route or storage changes
- [x] The manual handover records contain the actual revisions, verification and per-stack rollback inputs

### 5. Helmfile conversion

Credential-free implementation and rehearsal are in progress. The plan-and-apply bundle this phase was first written around is retired by [#95](https://github.com/jaegertracing/jaeger-demo/issues/95), together with bundle custody ([#71](https://github.com/jaegertracing/jaeger-demo/issues/71), [#72](https://github.com/jaegertracing/jaeger-demo/issues/72), [#73](https://github.com/jaegertracing/jaeger-demo/issues/73)) and the runtime decision in [#69](https://github.com/jaegertracing/jaeger-demo/issues/69).

Issues: [jaeger-demo#1](https://github.com/jaegertracing/jaeger-demo/issues/1).

The candidate configuration renders all six application releases and four namespace-local companion releases. `make validate` compares the application manifests against the extracted scripts and schema-checks both sets. The ownership rehearsal runs on a full Kubernetes node in PR CI. The release phase rehearsal covers healthy no-op applies, retry after a failed release, and pending-state blocking. The frozen-bundle tooling and tests built for the earlier contract remain in the repository until a separate issue removes or repurposes them. The complete live-equivalence inventory ([#70](https://github.com/jaegertracing/jaeger-demo/issues/70)) and production adoption are outstanding. [Application releases](helmfile-applications.md) · [Companion rehearsal](helmfile-companions.md)

Requires: verbatim-script-handover, core-hotrod-fixture, and an accepted direct scheduled run.

Deliverables:

- Pinned/checksummed toolchain, six labeled application releases and allowlisted namespace-local companion resources
- `helmfile apply` from the deploy workflow in place of the scripts: image resolution at deploy time, chart archive verification, compatibility validation before any mutation, failed/pending state handling, OCI-before-OTel verification and checks even on empty diffs
- Complete sanitized equivalence fixture, live drift report, deployer role coverage of the Helmfile render, and kind regression/adoption/rollback tests
- Delete backup/restore tooling and recovery gate; retain volume expansion and explicit constrained manual reset
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
- Daily schedule, first run: `DEMO_SCHEDULE_ENABLED` was set to `true` on November 20 at 12:44 UTC. [Run 44742195163](https://github.com/jaegertracing/jaeger-demo/actions/runs/44742195163) signalled the 13:00 UTC slot at configuration `1819e9a` and reported "Protected executor verified bundle 406fe1a80c6098002af4fe9ee7f9cd3993f1630205cb24225433c185fd2018e5" at 13:14:57 UTC. The executor image for that commit was [published](https://github.com/jaegertracing/jaeger-demo/actions/runs/44619647169) on November 19. Operator acceptance of this run, which reconciles the six releases and five component fingerprints and verifies fresh traces, public health, TLS and storage bindings ([commissioning and first-run criteria](daily-production-install-review.md)), is not recorded yet.
- Daily schedule, November 21: [run 44907484823](https://github.com/jaegertracing/jaeger-demo/actions/runs/44907484823) at configuration `ebbb98b` failed at 13:10:41 UTC with "Invalid reviewed execution engine or schedule interval". No preparation had been published for the slot. An operator read the cluster the same day, without changing it: the 12:20 UTC preparer Job started at 12:20:00 and failed at 13:00:00 UTC with `DeadlineExceeded`, its 40-minute limit; the node that runs the executor held 31 running pods against a limit of 31, the newest two created by the November 20 run; and the preparer has a required affinity to that node, because both mount the same single-attach volume. The request record was still the November 20 slot in state `succeeded`, so the requester did not signal, and the checkpoint named no lock holder. The scheduler's events had expired, so the reason the pod stayed unscheduled is inferred from the full node, not read. A refusal over guarded files exits within a minute and would not reach the deadline. Separately, the installed executor image was still the one for `1819e9a`, while merges after it had changed four executor-guarded files (both `deploy-all.sh`, `deploy.py` and `verify_trace.py`), so that preparation would have been refused had it started. The image [published](https://github.com/jaegertracing/jaeger-demo/actions/runs/44849897776) for `a06fb5d` was not installed, and later merges changed further guarded files. The next slot needs a free pod slot on the executor's node and an installed image that matches `main` ([guarded files](releases.md#merging-a-change-to-guarded-files), [a preparation that did not run](releases.md#failure-handling)). [#81](https://github.com/jaegertracing/jaeger-demo/issues/81) tracks the misleading message and the late report.
- Daily schedule, November 22: [run 45026014303](https://github.com/jaegertracing/jaeger-demo/actions/runs/45026014303) failed with no preparation published for the slot. [#94](https://github.com/jaegertracing/jaeger-demo/issues/94) records the cause read from the cluster: after [#56](https://github.com/jaegertracing/jaeger-demo/pull/56) changed the object inventory, the executor image was reinstalled but its permissions and admission guards were not, so the preparer could not read the new OTel application objects.
- Schedule paused: `DEMO_SCHEDULE_ENABLED` has been `false` since November 23 at 00:15 UTC. Whether the preparer is suspended and the executor stopped is not recorded here yet; [executor retirement](executor-retirement.md#before-the-first-direct-run) steps 2 to 4 cover them.
- Direct deployment ([#95](https://github.com/jaegertracing/jaeger-demo/issues/95), Option A): chart archive verification ([#96](https://github.com/jaegertracing/jaeger-demo/pull/96)), the deployer role ([#97](https://github.com/jaegertracing/jaeger-demo/pull/97)) and the workflow change define the path. None of it is production evidence. Open gates, in order: apply the role and bind the manual identity with a clean `check`; one accepted manual upgrade per component, monitoring first ([#87](https://github.com/jaegertracing/jaeger-demo/issues/87)); bind the scheduled identity; enable the schedule; accept the first direct scheduled run; then remove the executor objects from the cluster and the executor code from the repository.
- Add delivering PR links, merged commits and evidence as phases complete. Do not mark planned work complete.
