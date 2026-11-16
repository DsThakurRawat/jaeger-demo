<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Hosted-demo migration plan

Status: implementation in progress. Updated November 15, 2026. No production transition has been approved or executed.

## Ownership and fixed constraints

AI drives implementation, testing, PR preparation and execution within the accepted scope. Human verification applies to the access plan, first script handover, first Helmfile adoption and final acceptance. Each review-feedback reply requires individual human approval under the organization AI Usage Policy.

Core retains product source, image publishing, ordinary examples and integration tests. The demo repository owns hosted configuration and deployment releases.

- Existing OKE cluster, public URLs, DNS, namespaces, PVCs and six application release names
- Certificate issuers externally managed
- No ingress-controller/cert-manager upgrade or AI activation
- Disposable traces do not justify automatic destructive resets
- Generated code and submodules untouched

## Execution contract

- Implementation stages start only after predecessor exits are met. The core HotROD fixture runs in parallel with extraction. Helmfile conversion waits for script handover and the fixture to merge; post-adoption refactors merge before core removal.
- During script handover, capture and enforce frozen inputs in an operator-verified deployment record; do not imply a generated plan/apply runner exists yet. Helmfile conversion implements the following contract: Plan produces an immutable, digest-addressed deployment bundle: configuration commit merged to main, chart/dependency pins, tool versions/checksums, image commits/resolved digests and exact image references, stack/component/release selection including prerequisites, rendered manifests, relevant live-state fingerprint, and verification/rollback expectations. Apply consumes this exact bundle and rejects changed inputs or relevant live drift; it does not re-resolve main or image tags.
- First ownership adoption freezes live chart, values/config and exact image references. Preserve supplemental UIDs/selectors/Service ClusterIPs/pod-template hashes. Do not silently convert existing application image tags to digest syntax during adoption. Verify no-op behavior, then promote digest-pinned candidate images in a distinct reviewed deployment.
- Merge the extraction preserving both imported authored history and seed-repository history. Do not squash or rebase away imported commits. Verify filtered source ancestors are reachable from main after merge.
- Existing functional defects block the first script handover and Helmfile adoption, not local implementation. Refresh a healthy baseline immediately before each production transition.
- Helmfile automatic apply remains disabled until the initial plan is reviewed and manual adoption is verified.
- Respect required checks and maintainer review; never bypass branch protections.
- Do not expose plaintext credentials. Review new access/identity changes before creating or broadening permissions.

## Phases

### 1. Foundation

Status: in progress.

Deliverables:

- Public Apache-2.0 repository and five prerequisite issues: completed
- Bootstrap CODEOWNERS, credential-free CI, contributor/agent and AI-policy guidance in the extraction PR
- Verify DCO and Renovate app coverage; configure required PR review and actual CI check names after successful bootstrap checks
- Version-control this migration plan with status, issue/PR links, decisions, and evidence

Exit criteria:

- [ ] Bootstrap checks exist and pass; required review/check settings are verified without bypassing inherited rules
- [ ] App installation/access gaps have explicit owners; deployment workflows remain manual-only and production access is not enabled prematurely

### 2. Extraction

Status: implementation in progress.

Issues: [jaeger#10189](https://github.com/jaegertracing/jaeger/issues/10189).

Deliverables:

- History-preserving extraction at a freshly resolved upstream main cutoff, including scripts/demo/** and all test dependencies
- Minimal documented adapters for upstream image SHAs, paths, environments and hosted-only Renovate rules
- Credential-free shell/Python/Renovate CI; production workflows main-only and manual-only
- README architecture/ownership, cutoff and pending-change list; contribution, access, script-release, rollback and troubleshooting documentation

Exit criteria:

- [ ] Local tests and PR CI pass; maintainer-reviewed PR merged to main using a history-preserving merge
- [ ] Imported authored ancestors and seed history are reachable from target main; source cutoff and adapter audit recorded
- [ ] Documentation commands validated; deployment-ready configuration commit pinned; no hosted core assets removed

### 3. Core hotrod fixture

Status: implementation in progress.

Issues: [jaeger#10190](https://github.com/jaegertracing/jaeger/issues/10190).

Deliverables:

- Minimal core-owned memory Jaeger/HotROD fixture using localhost:5000 images and pullPolicy Never
- Independent config.yaml and ui-config.json test inputs, regression tests, local invocation/cleanup/troubleshooting documentation
- Keep product image publishing and Docker test behavior intact

Exit criteria:

- [ ] Core make fmt, make lint, make test and appropriate patch coverage pass
- [ ] Docker and Kubernetes HotROD CI pass without hosted tooling; reviewed PR merged to core main

### 4. Verbatim script handover

Status: pending.

Requires: foundation, extraction.

Deliverables:

- Refresh healthy functional baseline for both demos; separately tracked failures must be resolved
- Prepare immutable handover inputs, resource/route/storage diff and precise rollback procedure
- Prepare access plan; after human verification, provision fresh new-repo OCI access and separate plan/apply identities, verify permissions without deployment writes
- Human verifies handover bundle; disable all three old workflows and drain running AND pending runs
- Revoke old deployment key only after draining; enable the new manual path and deploy the verified bundle
- Verify both stacks, then enable the script schedule and verify one scheduled refresh

Exit criteria:

- [ ] Healthy baseline verified before first production change and recorded with bundle digest
- [ ] Human-verified handover uses merged main and exact frozen inputs; old runs drained and old key revoked
- [ ] Manual deployment and scheduled refresh succeed without unexplained resource, route or storage changes
- [ ] Runbook and release record include actual revisions, verification and rollback evidence

### 5. Helmfile conversion

Status: pending.

Issues: [jaeger-demo#1](https://github.com/jaegertracing/jaeger-demo/issues/1).

Requires: verbatim-script-handover, core-hotrod-fixture.

Deliverables:

- Pinned/checksummed toolchain, six labeled application releases and allowlisted namespace-local companion resources
- Immutable plan/apply bundle, independent image resolution, compatibility validation before any mutation, failed/pending state handling, OCI-before-OTel verification and checks even on empty diffs
- Complete sanitized equivalence fixture, live drift checks, read-only plan RBAC and kind regression/adoption/rollback tests
- Delete backup/restore tooling and recovery gate; retain volume expansion and explicit constrained manual reset
- Native Helmfile Renovate, daily checks, seven-day release age, reviewed updates without automerge
- Architecture, configuration/selectors, access/RBAC, release/promotion/rollback, maintenance/reset/incidents and troubleshooting runbooks

Exit criteria:

- [ ] All local/kind/schema/render/equivalence checks pass, including unchanged adoption identities and pod templates
- [ ] Last release-writer Helm major established; digest support confirmed; preliminary inventory gaps resolved
- [ ] Runbook commands validated and adoption/rollback rehearsed; reviewed PR merged to main
- [ ] Automatic apply remains disabled; manual adoption configuration commit and bundle pinned

### 6. Manual helmfile adoption

Status: pending.

Requires: helmfile-conversion.

Deliverables:

- Refresh healthy baseline, complete inventory and relevant live drift report
- Human verifies immutable bundle, adoption allowlist, exact live references and rollback revisions
- Adopt only allowlisted supplemental resources with keep policy; first companion install without atomic; preserve live application inputs
- Verify OCI before OTel mutation and all functional/resource invariants; perform a second no-op apply

Exit criteria:

- [ ] Initial manual adoption and no-op apply verified with unchanged supplemental UIDs/selectors/ClusterIPs/pod-template hashes
- [ ] No unexplained application rollout or resource/storage/route drift
- [ ] Human verification of successful adoption unlocks guarded automatic Helmfile operations

### 7. Promotion and acceptance

Status: pending.

Requires: manual-helmfile-adoption.

Deliverables:

- Enable guarded schedule only after manual adoption verification
- Promote digest-pinned candidate images separately from ownership adoption, using validated immutable bundles
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
- Workflow summaries and documented durable records include bundle digest, selected/changed releases, checks and outcomes, prior Helm revisions and rollback inputs. Define retention before operational handover.
- Docs ship with implementation, tested examples and stage exit evidence. Cover architecture, prerequisites, contributor/agent workflow, configuration, access, releases/promotion, maintenance/reset, rollback and incidents.
- Reviewed configuration/chart changes and accepted scheduled image refreshes run under standing authorization after the initial gates. A changed access scope, destructive reset or departure from documented invariants requires a concrete decision.

## Current evidence and unresolved gates

- Extraction source cutoff: [bcef01d0c848d5be0416c2eee117d5d306416dd7](https://github.com/jaegertracing/jaeger/commit/bcef01d0c848d5be0416c2eee117d5d306416dd7). Independent verification confirmed all 51 imported file blobs match that source and both imported and seed histories are reachable from the import merge. See the README for pending changes to rehome.
- A preliminary sanitized live inventory was collected November 15, 2026. It omits embedded configuration and is not the full equivalence fixture.
- Checkout SPM/traffic health has a separate investigation. A fresh healthy baseline is required before either production transition.
- The Cloud Shell Helm client is not proof of the last release writer; establish the writer major before pinning Helm.
- DCO/Renovate app coverage and required repository checks remain to be verified.
- Add delivering PR links, merged commits and evidence as phases complete. Do not mark planned work complete.
