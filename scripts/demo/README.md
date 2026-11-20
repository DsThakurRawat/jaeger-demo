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

Manual deployment runs use the reviewed `demo-apply` environment. The 13:00 UTC
job on main uses `demo-scheduled` after activation through the
[handover runbook](../../docs/cutover.md#review-scheduled-scope).

`deploy.py` fingerprints each component's deployment inputs and records successful
revisions in the `default/jaeger-demo-deployment-revisions` ConfigMap. Failed
revisions are not recorded, so the next reconciliation retries them. The daily
run also refreshes Jaeger and HotROD snapshots in both demos. A missing component key selects that component. Scheduled runs require the
existing revision ConfigMap and existing Helm releases. A partial failure does
not prevent independent components from deploying, but OpenSearch failure blocks
the dependent OTel Jaeger and application updates in that run.

Deployment, manual recovery and volume expansion share one workflow concurrency
lock. When the daily job selects OpenSearch, it upgrades the live StatefulSet
on its existing PVC, waits for readiness, and then upgrades Dashboards. The job
checks the database and public Dashboards versions and sends a fresh trace
through the collector and query service before recording success. Historical
synthetic traces are disposable. A destructive reset needs explicit approval.

An OpenSearch deployment requested manually defaults to a retained snapshot and
isolated restore test. Failure diagnostics and restore Pod cleanup belong to
manual runs that started recovery. An isolated manual OpenSearch upgrade from
main can explicitly waive this test. The standalone [recovery
procedure](../../examples/otel-demo/OPENSEARCH_RECOVERY.md) describes the
storage, access and capacity it uses.

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
without updating dependencies, run `make renovate-discovery`, which validates
the configuration and checks Renovate's credential-free extraction. Kubernetes
and OCI infrastructure/add-on maintenance is outside this workflow.
