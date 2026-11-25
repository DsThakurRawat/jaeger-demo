<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Ingress-controller dependency inventory

Source inventory for [#41](https://github.com/jaegertracing/jaeger-demo/issues/41).
This records repository inputs, not installed cluster state or a selected
migration. It does not authorize changing the externally owned controller,
cert-manager, public routes or certificate issuers. The
[migration constraints](MIGRATION.md#ownership-and-fixed-constraints) still apply.

## Operator information

| Item | Status |
| --- | --- |
| Controller owner and operational contact | The cluster administrator (`jkowall`); see the [migration plan](MIGRATION.md#ownership-and-fixed-constraints) |
| Installed controller distribution and version | Community ingress-nginx from its static manifest, retired upstream with no patch stream; recorded in the [migration plan](MIGRATION.md#ownership-and-fixed-constraints). The exact version is not published here |
| Existing replacement plan | None. No replacement is planned in this migration |
| Replacement choice and timing relative to Helmfile adoption | Decided: keep the current ingress-nginx controller as an accepted risk for this public demo. Revisit if an exploitable ingress-nginx CVE is published or when the cluster is rebuilt |

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
defines the scheduled and manual identities' direct permissions. The
[chart object inventory](../ops/rbac/chart-objects.json) and script requests in
[`deployer_access.py`](../scripts/demo/deployer_access.py) explain those rules;
they are not an object-level allowlist. The same script can compare live
SubjectAccessReview answers with required requests and a finite denied-request
list. This inventory reports no cluster execution of that check.

These are guardrails against mistakes, not strong identity isolation. As
[What a holder can reach](deployer-access.md#what-a-holder-can-reach) explains,
RBAC bind/escalate permissions, workloads and admission webhooks can let a
hostile holder reach cluster-administrator access. The
[live-access check caveats](deployer-access.md#verify-the-live-access) also apply:
group-derived access is not evaluated unless the groups are supplied, and the
self-check covers required access only. A clean result does not prove that an
identity cannot reach beyond the listed requests.

The [companion adoption procedure](helmfile-companions.md) also depends on the
named objects. A replacement proposal must account for these controls and any
new API kinds; it must not weaken the deployment guardrails to make a
migration pass.

## Decision boundary

The inventory can be reviewed independently of selecting Gateway API, another
Ingress controller or an OCI-specific service. None is selected here. The owner
and distribution are recorded above. The owner has decided to keep the current
community ingress-nginx controller. No replacement is planned in this migration.
This is an accepted risk for a public demo whose data is disposable and which
holds no credentials behind its routes. Revisit the decision if an exploitable
ingress-nginx CVE is published, or when the cluster is rebuilt. Any future
replacement needs its own proposal that defines route, redirect, TLS renewal and
rollback evidence. The existing production constraints remain unchanged.
