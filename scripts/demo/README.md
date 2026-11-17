<!--
Copyright (c) 2026 The Jaeger Authors.
SPDX-License-Identifier: Apache-2.0
-->

# Hosted demo maintenance

Renovate checks the two hosted demo directories daily. Releases must be at least seven days old, and maintainers review
every update. OpenSearch and Dashboards remain a matched chart pair. Other updates
are grouped by deployment component. Charts own their bundled image versions;
direct image overrides and load-generator requirements are tracked separately.
PostgreSQL, Valkey, and OpenSearch stay within their configured major lines until
a reviewed migration or compatibility change updates that constraint.

The extracted deployment workflow is manual-only and restricted to main until the
[ownership handover](../../docs/cutover.md) passes. Its daily trigger is present
only as a comment. After verified activation, the existing reconciler operates as
described below.
`deploy.py` fingerprints each component's deployment inputs and records successful
revisions in the `default/jaeger-demo-deployment-revisions` ConfigMap. Failed
revisions are not recorded, so the next reconciliation retries them. The daily
run also refreshes Jaeger and HotROD snapshots in both demos. New installations
without revision records reconcile every component once. A partial failure does
not prevent independent components from deploying, but OpenSearch failure blocks
the dependent OTel Jaeger and application updates in that run.

Deployment, recovery, and volume expansion share the existing workflow concurrency
lock. Database updates run the existing retained snapshot and isolated restore
test within that lock, then the deployment script checks the recovery point and
upgrades OpenSearch before Dashboards. A version check and a fresh collector-to-
query trace check must pass before recording the revision. Recovery failure stops
the update. Automated runs never waive recovery or perform a clean reinstall.

The deployment job has a time limit sized for a full-stack run that includes the
restore test, and it removes that run's disposable restore pod when it ends,
including after a failure or cancellation.

The manual `demo_stack`, `otel_deploy_scope`, image overrides, `deploy_mode`, and
recovery inputs are preserved. The historical `otel_deploy_scope` input also
accepts `monitoring` for an isolated OCI monitoring upgrade. Invalid stack/scope
pairs fail. Direct script callers can select a scope through
`JAEGER_DEMO_DEPLOY_SCOPE`; the legacy OTel environment variable remains accepted.

Chart upgrades are separate reviewed changes; do not bundle them with the initial
repository ownership handover. Keep component updates separate. Major database
changes require a tested migration in their reviewed change. Existing missing
OCI monitoring releases and pending Helm operations must be repaired under
[core #10002](https://github.com/jaegertracing/jaeger/issues/10002); an upgrade reports that failure instead of skipping monitoring and
reporting success. A `failed` monitoring release is upgraded in place by the
next reconciliation or `monitoring`-scope run.

Run `make lint` and `SHUNIT2=/path/to/shunit2 make test`; the latter runs the
maintenance tests through `scripts/utils/run-tests.sh`. To inspect discovery
without updating dependencies, use Renovate's local platform with
`--dry-run=extract`. Kubernetes and OCI infrastructure/add-on maintenance is
outside this workflow.
