<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Jaeger hosted demos

Configuration and operations for the public Jaeger demos on the existing Oracle
Kubernetes Engine (OKE) cluster. Jaeger and HotROD source, image publishing,
product releases and integration tests remain in
[jaegertracing/jaeger](https://github.com/jaegertracing/jaeger).

The daily 13:00 UTC job covers both Jaeger stacks, OpenSearch and Dashboards,
the OTel sample application, and OCI monitoring through a
[protected deployment request](docs/daily-update-request.md) to an
[in-cluster executor](docs/in-cluster-executor.md). For current migration
status and handover evidence, see [docs/MIGRATION.md](docs/MIGRATION.md).
Schedule activation is tracked in the
[handover runbook](docs/cutover.md#review-scheduled-scope).

## Public endpoints

| URL | Stack | Backing service | Storage |
| --- | --- | --- | --- |
| https://demo.jaegertracing.io/jaeger | OCI | Jaeger query UI | In-memory |
| https://demo.jaegertracing.io/grafana | OCI | Grafana (Prometheus stack) | Prometheus |
| https://demo.jaegertracing.io/hotrod | OCI | HotROD sample application | In-memory traces in Jaeger |
| https://demo.jaegertracing.io/ | OCI | Permanent redirect to https://www.jaegertracing.io/demo/ | None |
| https://jaeger.demo.jaegertracing.io | OTel | Jaeger query UI | Traces in OpenSearch |
| https://hotrod.demo.jaegertracing.io | OTel | HotROD sample application | Traces in OpenSearch (via Jaeger) |
| https://opensearch.demo.jaegertracing.io | OTel | OpenSearch Dashboards | OpenSearch |
| https://shop.demo.jaegertracing.io | OTel | OpenTelemetry Demo storefront (`frontend-proxy`) | Traces in OpenSearch (via Jaeger); shop data stays in the demo's own PostgreSQL and Valkey |

Routes are defined in [examples/oci/ingress.yaml](examples/oci/ingress.yaml),
[ingress-jaeger.yaml](examples/otel-demo/ingress/ingress-jaeger.yaml),
[ingress-opensearch.yaml](examples/otel-demo/ingress/ingress-opensearch.yaml) and
[ingress-otel-demo.yaml](examples/otel-demo/ingress/ingress-otel-demo.yaml).

## Architecture and ownership

| Stack | Namespace | Application releases | Configuration |
| --- | --- | --- | --- |
| OCI, memory storage and HotROD | `default` | `jaeger`, `prometheus` | [examples/oci](examples/oci) |
| OTel, OpenSearch storage | `jaeger` | `jaeger` | [examples/otel-demo](examples/otel-demo) |
| OTel storage and dashboards | `opensearch` | `opensearch`, `opensearch-dashboards` | [examples/otel-demo](examples/otel-demo) |
| OTel sample application | `otel-demo` | `otel-demo` | [examples/otel-demo](examples/otel-demo) |

The deployment workflow uses [scripts/demo/deploy.py](scripts/demo/deploy.py) to
select changed components and invoke the stack scripts. The snapshot resolver
selects Jaeger and HotROD images independently from upstream core commits. This
repository's commit identifies configuration, never a product image.

Existing namespaces, release names, routes, Service addresses and PVCs are
preserved. Certificate issuers and the shared cluster, ingress controller and
cert-manager are externally owned. Their installation examples are retained as
history; they are not migration instructions. Cluster provisioning, DNS changes
and AI activation are outside this migration.

## Development

Local tests require Bash, Python 3, Git, jq, Helm, ShellCheck, actionlint and
shunit2. They use mocks and render public Helm charts without production
credentials. Chart rendering requires network access.

```bash
# Point at a shunit2 v2.1.8 checkout, or follow CONTRIBUTING.md to create one.
export SHUNIT2=/path/to/shunit2
make lint
make test
```

[CONTRIBUTING.md](CONTRIBUTING.md) covers setup, credential-free Renovate discovery,
DCO, issues and required checks. [AGENTS.md](AGENTS.md) covers agent work.

## Operations and releases

- [Migration phases, decisions and exit criteria](docs/MIGRATION.md)
- [Access and ownership handover](docs/cutover.md)
- [Deployment releases, provenance, verification and rollback](docs/releases.md)
- [Current script reconciler and maintenance](scripts/demo/README.md)
- [OCI stack commands](examples/oci/README.md)
- [OTel stack commands](examples/otel-demo/README.md)

Use the handover and release runbooks for the existing installation. Do not run
historical installation, `clean`, recovery or cleanup commands merely to complete
the migration. The daily OpenSearch upgrade preserves its existing PVC and
verifies new trace ingestion and query. Generated trace history is disposable.
During Stage 1, maintainers can separately request a manual tested backup. Retirement of that
tooling and the narrow manual reset belong to [Helmfile conversion](https://github.com/jaegertracing/jaeger-demo/issues/1).

## Extraction provenance

Imported from upstream `main` at
[`bcef01d0c848d5be0416c2eee117d5d306416dd7`](https://github.com/jaegertracing/jaeger/commit/bcef01d0c848d5be0416c2eee117d5d306416dd7).
The filtered history retains 55 authored commits and the new repository's seed
history. See [extraction.md](docs/extraction.md) for the path list, commit mapping,
minimal adapters and verification. Merge the extraction PR with a merge commit;
squashing would remove the imported ancestry from `main`.

### To rehome

Open core PRs touching imported paths at the cutoff are listed below. Rehome only
hosted-demo changes after coordinating the original review; this list does not
approve or merge their proposed behavior. The analytics change
[#10141](https://github.com/jaegertracing/jaeger/pull/10141) was already merged and
is included in the cutoff.

| Core PR | Relevant work |
| --- | --- |
| [#10131](https://github.com/jaegertracing/jaeger/pull/10131) | Bounded AI assistant; activation remains outside migration scope |
| [#10174](https://github.com/jaegertracing/jaeger/pull/10174) | Routine disposable-trace policy for the hosted schedule is implemented in [demo #15](https://github.com/jaegertracing/jaeger-demo/pull/15). Manual recovery has its own opt-in path. |
| [#10180](https://github.com/jaegertracing/jaeger/pull/10180) | Manual restore Pod startup, rehomed in [demo #14](https://github.com/jaegertracing/jaeger-demo/pull/14) |
| [#10031](https://github.com/jaegertracing/jaeger/pull/10031) | Grafana probes and demo smoke checks |
| [#10172](https://github.com/jaegertracing/jaeger/pull/10172) | Ported the recovery API helpers and their regression tests. See the [recovery procedure](examples/otel-demo/OPENSEARCH_RECOVERY.md). |
| [#10155](https://github.com/jaegertracing/jaeger/pull/10155) | Hosted configuration portions of the batch migration; core configuration stays in core |
| [#9149](https://github.com/jaegertracing/jaeger/pull/9149) | Inspect only its `ci-deploy-demo.yml` hunk; the critical-path work stays in core |
