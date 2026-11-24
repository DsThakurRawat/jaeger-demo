<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Extraction provenance

The source cutoff is upstream `jaegertracing/jaeger` main at
`bcef01d0c848d5be0416c2eee117d5d306416dd7`. A fresh full-history main clone was
filtered with git-filter-repo 2.47.0 using only:

```text
examples/oci/
examples/otel-demo/
scripts/demo/
.github/workflows/ci-deploy-demo.yml
.github/workflows/ci-opensearch-recovery.yml
.github/workflows/ci-opensearch-volume-expand.yml
scripts/utils/resolve-demo-snapshot-tags.sh
scripts/utils/resolve-demo-snapshot-tags.test.sh
```

The result has 55 authored commits, ending at
`e84b014daaaac1a55939264ccddb46b93a2e4fed`. Before adapters, all 51 imported file
blobs matched the source cutoff. [imported-commits.tsv](imported-commits.tsv) maps
original commit IDs to filtered IDs; filtering changes commit IDs because the
trees and parent chains differ. Authors and original messages were retained.

An unrelated-history merge retains the target repository's seed commit
`f02899afadb1a38c30363b2b7c8909b2bdd45488` as well as the filtered history. New
integration and adapter commits have DCO sign-off. Some historical source commits
lack sign-off trailers; preserve their original messages and provenance instead
of adding certifications on behalf of their authors. DCO handling for historical
imports remains a maintainer decision if the check reports them.

## Minimal adapters

1. Remove the source repository's push and schedule triggers. Production workflows
   are main-only, use `demo-apply`, and share the existing concurrency group.
   Permissions are restricted to repository-content reads. The commented daily
   schedule is enabled only after verified handover.
2. Resolve `jaegertracing/jaeger` main explicitly before image selection. Remove the
   resolver's `GITHUB_SHA` fallback and workflow image defaults based on configuration
   SHA. Preserve independent image overrides and the existing resolver behavior.
3. Add `expected_config_sha` for the reviewed handover. Reject mismatched or malformed
   full SHAs before OKE setup. Gate failure diagnostics and cleanup on successful
   access configuration so rejected inputs cannot invoke cluster commands.
4. Retain the example directory layout, including the OTel reference to OCI's UI
   configuration. No application configuration, chart pins or deployment script
   behavior changes are included.
5. Move only hosted Renovate managers/rules and applicable shared update policy.
   Exclude unrelated core Go, Docker Compose and tooling dependencies. Keep daily
   reviewed updates, seven-day release age and existing major-version constraints.
6. Add a demo-only test runner and credential-free PR CI rather than importing core's
   unrelated release/Goose/Cassandra test runner. Add ownership/contributor/agent
   guidance, migration and operational documentation.

No Helmfile implementation, recovery-policy removal, production credential change,
workflow activation, core asset removal or live deployment is part of extraction.
The recovery workflow, script and policy in the path list above were removed
later ([#76](https://github.com/jaegertracing/jaeger-demo/issues/76)).

## Pending source changes

The README's rehome list was refreshed from all 198 open core PRs on November 15,
2026. No PR file list was truncated. Seven open PRs touched imported paths. Inspect
only their relevant hosted hunks and preserve original review context. The merged
analytics PR #10141 is already represented in the cutoff.

## Merge verification

Use a merge commit for the extraction PR. Do not squash or rebase away its imported
ancestry. After merge, fetch target `main` and verify both histories remain reachable:

```bash
git fetch origin main
git merge-base --is-ancestor e84b014daaaac1a55939264ccddb46b93a2e4fed origin/main
git merge-base --is-ancestor f02899afadb1a38c30363b2b7c8909b2bdd45488 origin/main
```

Record the merged configuration commit and successful checks in the migration plan.
Code merge does not close core issue #10189: that issue also requires the separately
verified script handover and scheduled refresh.
