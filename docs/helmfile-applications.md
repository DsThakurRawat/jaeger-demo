<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Helmfile application releases

`helmfile.yaml` describes the six existing application releases. The current production deployment uses the extracted scripts. The commands below render public configuration for review.

| Stack and component | Helm release | Prerequisites for the component |
| --- | --- | --- |
| OCI monitoring | `default/prometheus` | None |
| OCI Jaeger | `default/jaeger` | `default/prometheus` supplies the SPM query backend |
| OTel OpenSearch | `opensearch/opensearch`, `opensearch/opensearch-dashboards` | Dashboards follows OpenSearch |
| OTel Jaeger | `jaeger/jaeger` | `opensearch/opensearch` stores traces |
| OTel application | `otel-demo/otel-demo` | `opensearch/opensearch` and `jaeger/jaeger` supply storage and ingestion |

The chart pins match the extracted deployment scripts. Both Jaeger releases use the Git revision declared in `scripts/demo/jaeger-chart.sh`, plus its chart's locked dependencies. The state reads the existing Jaeger config and UI files with Helm's `--set-file` handling. Both Jaeger releases retain atomic upgrades. OpenSearch and Dashboards retain their non-atomic upgrade behavior and existing namespace and storage settings.

## Render a component

Run the following from the repository root with the pinned tools on `PATH`, a private Helm cache and an empty kubeconfig. Preparing charts requires access to public chart repositories.

```bash
export KUBECONFIG=/dev/null
mkdir -p .tools
source scripts/demo/jaeger-chart.sh
prepare_jaeger_chart "$PWD/.tools/helm-charts"
helmfile -f helmfile.yaml repos
```

Pass the exact image values from the reviewed inputs. The following example illustrates the OTel stack with synthetic tags suitable for rendering.

```yaml
images:
  otel:
    jaeger:
      repository: jaegertracing/jaeger-snapshot
      tag: "0123456"
      pullPolicy: Always
    hotrod:
      repository: jaegertracing/example-hotrod-snapshot
      tag: "abcdef0"
      pullPolicy: Always
```

Save the example as `.tools/images.yaml`. For an OCI or all-stack render, supply the same two image records under `images.oci`. During initial adoption, preserve each live repository, tag and pull policy. A subsequent reviewed promotion can use `tag@sha256:<digest>` in the tag field. All three fields are required for each selected Jaeger or HotROD image.

```bash
python3 scripts/demo/helmfile_selection.py --stack otel --component otel-demo
helmfile --file helmfile.yaml --state-values-file .tools/images.yaml \
  --selector stack=otel,component=otel-demo --skip-deps template \
  --include-transitive-needs --include-crds --kube-version 1.36.1
```

The first command prints all three release identities in dependency order. Both commands use the same stack and component. Always specify the stack when selecting a component because both Jaeger releases are named `jaeger`. Render OCI and OTel separately when evaluating an all-stack change. The production runner must verify OCI's functional checks before beginning OTel mutations.

## Verify script equivalence

```bash
python3 scripts/demo/rehearsals/helmfile-applications/equivalence.py
```

This credential-free check uses the extracted scripts' chart versions and equivalent Helm arguments as its reference. It renders each release using Helm and Helmfile, compares full Kubernetes objects by identity including CRDs and hooks, checks both application's exact image references and pull policies, and compares the OTel application selection with Helmfile's native transitive rendering. A synthetic Grafana password makes its two offline renders deterministic. Production planning must compare against the current named Grafana Secret and preserve its UID and payload. Live equivalence, supplemental resource adoption, and rollout verification are separate operator checks. [Adoption contract](MIGRATION.md)

## Public health checks

Run `python3 scripts/demo/stack_health.py --stack oci` or `--stack otel` to check the Jaeger UI and its configured analytics ID, the stack's HTTPS routes, current frontend or checkout traces, and finite p95 latency in a completed SPM bucket with positive calls. Calls and latency use the same UTC end time and must have matching sample timestamps. The OCI check also inspects Grafana database health. The OTel check verifies Dashboards status and the storefront. The output contains service names, freshness and aggregate metrics. The checker reads existing traffic. A controlled write/read and cluster rollout checks are additional release gates.

## Pull request render summary

The **Rendered resource changes** pull request job renders the exact base and head
commits using the head’s pinned toolchain and the public image and Grafana
fixtures from the equivalence rehearsal. Each checkout supplies its own Jaeger
chart pin, chart versions, values, templates and supplemental objects. For the
initial migration, the base projection uses the six script-era release arguments
and the twelve supplemental resources in their existing namespaces. The summary
identifies that fallback.

The GitHub Actions job summary lists added, modified and removed resource
identities. Comparison includes Secret contents in process memory. The job
publishes only counts and resource identities. Its children have an isolated
home and Helm configuration, an empty kubeconfig, no cloud credentials and no
access to a GitHub write token. Both checkouts disable credential persistence.

To reproduce, supply separate clean checkouts for the old and new commits. Run in
the candidate checkout with PyYAML installed. The pinned chart repositories need
public network access.

```sh
python3 scripts/demo/helmfile_tools.py run -- python3 scripts/demo/pr_render_diff.py \
  --base /path/to/base --head "$PWD"
```
