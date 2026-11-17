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
  Do not copy the old private key. Prepare separate read-only plan and write-capable
  apply identities in `demo-plan` and `demo-apply`, restricted to main. The extraction
  workflows use the apply environment; a generated read-only plan workflow is later
  Helmfile work. Verify permissions without deployment writes before retiring access.

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
helm list --all-namespaces --all
helm history jaeger --namespace default -o json
helm history jaeger --namespace jaeger -o json
kubectl get pvc --all-namespaces
kubectl get ingress --all-namespaces
```

These commands are partial checks, not a complete inventory collector. Review any
additional captured output before sharing it.

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
4. Revoke the old OCI key only after the old runs have drained and new access has
   been verified. Record the credential identity and revocation result without the
   key value. Retain a tested route to the cluster for recovery.
5. Enable the new manual deployment path if GitHub has disabled it. Dispatch only
   the reviewed inputs from main. For the narrow both-demo handover, use
   `demo_stack=all`, `otel_deploy_scope=jaeger`, `deploy_mode=upgrade`, and independent
   explicit Jaeger/HotROD commit tags. Supply the full configuration SHA in
   `expected_config_sha`; the workflow fails before OKE setup if main has changed.
   Set these variables from the verified record, then dispatch:

   ```bash
   : "${config_sha:?Set the reviewed full configuration SHA}"
   : "${jaeger_tag:?Set the reviewed Jaeger commit tag}"
   : "${hotrod_tag:?Set the reviewed HotROD commit tag}"
   gh workflow run ci-deploy-demo.yml --repo jaegertracing/jaeger-demo --ref main \
     -f "expected_config_sha=$config_sha" \
     -f demo_stack=all -f otel_deploy_scope=jaeger -f deploy_mode=upgrade \
     -f "jaeger_image_tag=$jaeger_tag" -f "hotrod_image_tag=$hotrod_tag" \
     -f opensearch_recovery=required
   ```

   Verify and record the run's `headSha` and inputs. A differing configuration
   revision requires re-verification, not removal of the guard.

   This selection deploys the two Jaeger components. It does not exercise a chart
   upgrade of OpenSearch, monitoring or the OTel application. The full release and
   public-behavior checks still cover the existing six-release installation.
6. Verify actual changes and all health checks in [releases.md](releases.md). The
   extracted scripts retain unconditional generator restarts and OTel HotROD
   Deployment replacement; review those expected effects rather than claiming this
   script handover preserves all pod or Deployment identities. Unchanged identity
   and pod-template adoption tests belong to the later Helmfile transition.
7. Enable the daily script schedule only after the manual deployment passes. Before
   enabling it, inspect the stored component fingerprints and intended selection;
   a changed or missing fingerprint may select additional components. Verify the
   first scheduled run and record its outcome. That completes Stage 1 operational
   handover.

Current recovery tooling remains present during extraction. Its waiver is limited
to a manual `main` / `otel` / `upgrade` / `opensearch` invocation. It is not accepted
for `all/all`. Do not use `clean` or bypass recovery checks to make handover pass.
Deletion of recovery tooling and the routine recovery gate is explicitly scheduled
for the Helmfile conversion.

## If the handover fails

Stop new automation first and retain diagnostics. Do not reactivate old workflows
while the new path can still write. Restore application revisions and supplemental
specs using the recorded scope, then verify health. See
[release failure handling](releases.md#failure-handling).

The old key stays revoked. If returning operational ownership to core is necessary,
verify a replacement scoped credential and the exact old configuration before
enabling that path. Do not copy back the retired key or enable both repositories.
Do not remove the legacy fingerprint ConfigMap during handover.

## Later Helmfile adoption

Helmfile conversion starts only after the script handover succeeds and the parallel
core HotROD fixture has merged. Before production adoption:

- Complete render/inventory equivalence and live drift checks, confirm the last
  release-writer Helm major and digest support, and pass the credential-free tests.
- Rehearse adoption and rollback in kind. Obtain a fresh healthy live baseline.
- Review the immutable deployment bundle and exact supplemental allowlist. Freeze
  existing application chart/config/image references during ownership adoption.
- Adopt only allowed ingresses, supplemental Services and generator resources into
  namespace-local companion releases. Reject foreign ownership; keep resource
  identities, selectors, ClusterIPs and pod-template hashes. Do not template PVCs,
  namespaces, snapshots or generated TLS secrets. The first companion installation
  must not use atomic rollback.
- Verify OCI before OTel mutation, then perform a second no-op apply. Promote
  digest-pinned candidate images separately after verified ownership adoption.

The implemented Helmfile PR must supply exact commands for that procedure and its
rollback. Companion rollback stops automation, removes only the recorded companion
release metadata to de-adopt kept objects, and reapplies captured specs. Never use
`helm uninstall` on a companion release as a de-adoption shortcut. Never treat an
OpenSearch downgrade as a generic rollback. The old scripts can be a fallback only
after de-adoption has completed.

Verified initial adoption unlocks guarded automation. Final acceptance requires
successful scheduled refresh and reviewed Renovate chart update, a healthy no-op
apply, at least 24 hours of observation, and verified documentation/release
procedures. Post-adoption refactors then merge before core assets and legacy
fingerprint state are retired.
