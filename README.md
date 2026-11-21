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

The diagram below maps each public route to its namespace and Helm release, then
follows the traces. Thin arrows from the ingress are public routes, thick arrows
carry traces, and dotted arrows are metrics, TLS or the root redirect. Routes come
from the Ingress manifests listed above. Trace endpoints come from the Jaeger
values, `config.yaml`, `jaeger-config.yaml`, `otel-demo-values.yaml` and the two
load generators.

```mermaid
flowchart LR
    visitors(("Visitors"))
    redirect["https#58;//www#46;jaegertracing#46;io/demo/"]

    subgraph cluster["OKE cluster (externally owned)"]
        ingress["Ingress controller, class nginx<br/>(externally owned)"]
        certs["cert-manager and letsencrypt-prod issuers<br/>(externally owned)"]

        subgraph ns_default["namespace default"]
            oci_gen["Load generator<br/>(supplemental manifest)"]
            oci_hotrod["HotROD<br/>release jaeger"]
            oci_jaeger["Jaeger, traces in memory<br/>release jaeger"]
            prometheus["Prometheus and Grafana<br/>release prometheus"]
        end

        subgraph ns_jaeger["namespace jaeger"]
            otel_gen["Load generator<br/>(supplemental manifest)"]
            otel_hotrod["HotROD<br/>release jaeger"]
            otel_jaeger["Jaeger<br/>release jaeger"]
        end

        subgraph ns_opensearch["namespace opensearch"]
            opensearch[("OpenSearch<br/>release opensearch")]
            dashboards["OpenSearch Dashboards<br/>release opensearch-dashboards"]
        end

        subgraph ns_otel_demo["namespace otel-demo"]
            shop["OpenTelemetry Demo, frontend-proxy<br/>release otel-demo"]
        end
    end

    visitors -- "HTTPS" --> ingress
    certs -. "TLS certificates" .-> ingress
    ingress -- "demo.jaegertracing.io/jaeger" --> oci_jaeger
    ingress -- "demo.jaegertracing.io/hotrod" --> oci_hotrod
    ingress -- "demo.jaegertracing.io/grafana" --> prometheus
    ingress -. "demo.jaegertracing.io/ permanent redirect" .-> redirect
    ingress -- "jaeger.demo.jaegertracing.io" --> otel_jaeger
    ingress -- "hotrod.demo.jaegertracing.io" --> otel_hotrod
    ingress -- "opensearch.demo.jaegertracing.io" --> dashboards
    ingress -- "shop.demo.jaegertracing.io" --> shop

    oci_gen -- "HTTP /hotrod/dispatch" --> oci_hotrod
    oci_hotrod == "OTLP HTTP :4318" ==> oci_jaeger
    prometheus -. "scrapes span metrics :8889" .-> oci_jaeger
    oci_jaeger -. "SPM queries :9090" .-> prometheus

    otel_gen -- "HTTP /dispatch" --> otel_hotrod
    otel_hotrod == "OTLP HTTP :4318" ==> otel_jaeger
    shop == "OTLP gRPC :4317 from the demo collector" ==> otel_jaeger
    otel_jaeger == "jaeger-main and jaeger-archive indices" ==> opensearch
    dashboards -- "queries" --> opensearch
```

The deployment workflow uses [scripts/demo/deploy.py](scripts/demo/deploy.py) to
select changed components and invoke the stack scripts. The snapshot resolver
selects Jaeger and HotROD images independently from upstream core commits. This
repository's commit identifies configuration, never a product image.

Two paths write to the cluster, and both hold the same writer lock. Maintainers
dispatch the manual path. The daily path is gated: it needs operator installation
of the preparer and executor, then separate activation approval, before the
`DEMO_SCHEDULE_ENABLED` switch lets the 13:00 UTC job signal. The
[executor sequence](docs/in-cluster-executor.md#daily-sequence) shows each step and
its failure handling.

```mermaid
flowchart TB
    subgraph manual["Manual path: ci-deploy-demo.yml, job deploy"]
        dispatch["workflow_dispatch on main"] --> approve["Environment demo-apply<br/>reviewer approval"]
        approve --> manual_lock["Acquire the writer lock<br/>once its protocol is installed"]
        manual_lock --> reconcile["deploy.py runs the stack scripts<br/>for the selected components"]
    end

    subgraph daily["Daily path, 13:00 UTC (gated on installation and activation)"]
        request["ci-deploy-demo.yml, job request-daily<br/>environment demo-scheduled<br/>runs when DEMO_SCHEDULE_ENABLED is true"]
        control["ConfigMap<br/>jaeger-demo-operations/daily-hosted-update"]
        preparer["jaeger-demo-preparer<br/>CronJob, 12:20 UTC"]
        custody[("Bundle custody")]
        executor["jaeger-demo-executor<br/>verifies the bundle, claims the writer lock"]
        oci["OCI first: default/prometheus, default/jaeger"]
        otel["OTel: opensearch, opensearch-dashboards,<br/>jaeger/jaeger, otel-demo"]
        outcomes[("Durable outcome records")]

        preparer -- "signed, encrypted dated bundle" --> custody
        preparer -- "ready request and receipt" --> control
        request -- "signal" --> control
        control -- "polled every 10 s" --> executor
        custody -- "verified and decrypted" --> executor
        executor -- "selected components" --> oci
        oci -- "health checks pass" --> otel
        executor --> outcomes
        executor -- "terminal state" --> control
        control -- "observed result" --> request
    end

    lock[("Writer lock on<br/>default/jaeger-demo-deployment-revisions")]
    slack["Slack #jaeger-operations"]
    observer["demo-run-observer.yml<br/>runs that ended before any job started"]
    helmfile["Helmfile candidate, engine helmfile-v1<br/>not in production"]

    manual_lock -.-> lock
    executor -.-> lock
    reconcile -- "failed, cancelled or timed out" --> slack
    request -- "failed, cancelled or timed out" --> slack
    observer --> slack

    style daily stroke-dasharray: 5 5
    style helmfile stroke-dasharray: 5 5
```

Dashed outlines mark the gated daily path and the Helmfile candidate. The Helmfile
candidate is validated locally and in PR CI; production continues to run the
extracted scripts until operator acceptance ([migration plan](docs/MIGRATION.md)).

Existing namespaces, release names, routes, Service addresses and PVCs are
preserved. Certificate issuers and the shared cluster, ingress controller and
cert-manager are externally owned. Their installation examples are retained as
history; they are not migration instructions. Cluster provisioning, DNS changes
and AI activation are outside this migration.

## Development

Local checks need Bash, Git, jq, Helm, ShellCheck, actionlint, shunit2, ruff and
Python 3 with PyYAML and cryptography. Use Python 3.11 or later: the
`pycparser` pin in
[oci-cli-constraints.txt](scripts/demo/oci-cli-constraints.txt), which
`cryptography` pulls in through `cffi`, requires it. The tests use mocks and
render public Helm charts without production credentials. Chart rendering
requires network access.

[CONTRIBUTING.md](CONTRIBUTING.md#local-checks) has the install commands, the
shunit2 checkout, `make lint`, `make test` and `make check`, plus credential-free
Renovate discovery, DCO, issues and required checks. [AGENTS.md](AGENTS.md) covers
agent work.

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
