<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Ingress-controller dependency inventory

Source inventory for [#41](https://github.com/jaegertracing/jaeger-demo/issues/41).
This records repository inputs, not installed cluster state or a selected
migration. It does not authorize changing the externally owned controller,
cert-manager, public routes or certificate issuers. The
[migration constraints](MIGRATION.md#ownership-and-fixed-constraints) still apply.

## Operator information still needed

| Item | Status |
| --- | --- |
| Controller owner and operational contact | Not established by this source inventory; operator confirmation needed |
| Installed controller distribution and version | Not verified; `ingressClassName: nginx` alone does not identify a running implementation or version |
| Existing replacement plan | Not verified; ask the controller owner before proposing a replacement |
| Replacement choice and timing relative to Helmfile adoption | Maintainer/operator decision still open |

## Five source Ingress objects

All five specify `ingressClassName: nginx`. The OCI file omits a namespace;
[companion inputs](../scripts/demo/companion_values.py) place its objects in
`default`. The other three files explicitly name their namespaces.

| Namespace / object | Source | Hosts and paths | TLS Secret |
| --- | --- | --- | --- |
| `default/jaeger-demo-ingress` | [OCI ingress](../examples/oci/ingress.yaml) | `demo.jaegertracing.io`: `/jaeger`, `/grafana`, `/hotrod`, all `Prefix` | `demo-jaeger-tls` |
| `default/jaeger-root-redirect` | [OCI ingress](../examples/oci/ingress.yaml) | `demo.jaegertracing.io`: `/`, `Prefix` | `demo-jaeger-tls` |
| `jaeger/jaeger-demo-ingress` | [Jaeger ingress](../examples/otel-demo/ingress/ingress-jaeger.yaml) | `jaeger.demo.jaegertracing.io` and `hotrod.demo.jaegertracing.io`: `/`, `Prefix` | `jaeger-demo-tls` |
| `opensearch/opensearch-demo-ingress` | [OpenSearch ingress](../examples/otel-demo/ingress/ingress-opensearch.yaml) | `opensearch.demo.jaegertracing.io`: `/`, `Prefix` | `opensearch-demo-tls` |
| `otel-demo/otel-demo-ingress` | [Shop ingress](../examples/otel-demo/ingress/ingress-otel-demo.yaml) | `shop.demo.jaegertracing.io`: `/`, `Prefix` | `otel-demo-tls` |

The OCI paths route to `jaeger-query:16686`, `prometheus-grafana:80` and
`jaeger-hotrod:80`, respectively. The root Ingress sets
`nginx.ingress.kubernetes.io/permanent-redirect` to
`https://www.jaegertracing.io/demo/`; its source declares a backend but intends
the redirect to take precedence. A replacement must verify actual redirect
behavior rather than assuming another controller understands that annotation.

The other source backends are `jaeger-query-clusterip:16686` and
`jaeger-hotrod:80`, `opensearch-dashboards:5601`, and `frontend-proxy:8080`.
This inventory does not prove current public reachability, routing behavior or
certificate validity.

## Certificate issuance dependencies

- The [OCI namespaced Issuer](../examples/oci/tls-cert/issuer.yaml),
  `letsencrypt-prod`, uses an HTTP-01 Ingress solver with
  `ingressClassName: nginx`. Both OCI Ingresses select it through
  `cert-manager.io/issuer`.
- The [OTel ClusterIssuer](../examples/otel-demo/ingress/clusterissuer-letsencrypt-prod.yaml),
  also named `letsencrypt-prod`, uses the same HTTP-01 solver class. The three
  OTel-stack Ingresses select it through `cert-manager.io/cluster-issuer`.

These are distinct issuer scopes. Changing application Ingress classes alone
would leave the challenge solvers dependent on the old class. Any eventual
migration needs an operator-reviewed issuance/renewal and rollback check for
both scopes, without assuming that copying an annotation or Secret name
preserves issuance.

## Existing controls to preserve

[Companion input generation](../scripts/demo/companion_values.py) includes the
five Ingresses but does not include either issuer manifest. Moving application
routes is therefore not the same operation as changing the externally managed
issuer/controller configuration.

Under direct deployment, the [deployer role](../ops/rbac/deployer.yaml)
scopes the scheduled and manual deployment identities to the six releases and
grants no access in the ingress-controller or cert-manager namespaces. The
[chart object inventory](../ops/rbac/chart-objects.json) records the objects
those identities may touch, and the
[denied-access list](../scripts/demo/deployer_access.py) proves against a live
cluster that neither identity can reach outside that scope. The
[companion adoption procedure](helmfile-companions.md) also depends on the
named objects. A replacement proposal must account for these controls and any
new API kinds; it must not weaken the deployment guardrails to make a
migration pass.

## Decision boundary

The inventory can be reviewed independently of selecting Gateway API, another
Ingress controller or an OCI-specific service. None is selected here. Before an
implementation, identify the controller owner/version and existing plan, agree
on timing relative to the Helmfile phases, and define route, redirect, TLS
renewal and rollback evidence. Until then, #41 remains open and the existing
production constraints remain unchanged.
