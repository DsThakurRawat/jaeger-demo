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
  --include-transitive-needs --include-crds --kube-version 1.37.0
```

The first command prints all three release identities in dependency order. Both commands use the same stack and component. Always specify the stack when selecting a component because both Jaeger releases are named `jaeger`. Render OCI and OTel separately when evaluating an all-stack change. The production runner must verify OCI's functional checks before beginning OTel mutations.

## Verify script equivalence

```bash
python3 scripts/demo/rehearsals/helmfile-applications/equivalence.py
```

This credential-free check uses the extracted scripts' chart versions and equivalent Helm arguments as its reference. It renders each release using Helm and Helmfile, compares full Kubernetes objects by identity including CRDs and hooks, checks both application's exact image references and pull policies, and compares the OTel application selection with Helmfile's native transitive rendering. A synthetic Grafana password makes its two offline renders deterministic. Production planning must compare against the current named Grafana Secret and preserve its UID and payload. Live equivalence, supplemental resource adoption, and rollout verification are separate operator checks. [Adoption contract](MIGRATION.md)

## Read live fingerprints and images

```bash
python3 scripts/demo/live_observation.py --kubeconfig "$KUBECONFIG" --context "$CONTEXT" \
  --object apps/v1:Deployment:default:jaeger --stack oci --stack otel
```

The command makes only `GET` requests, through a local `kubectl proxy` that rejects every other method. For each `--object` identity (`apiVersion:Kind:namespace:name`, with `-` as the namespace of a cluster-scoped object) it resolves the resource through server discovery and prints the object's UID and a SHA-256 of its content without `status` and transient metadata, or `null` when the object is absent. For each `--stack` it prints the Jaeger and HotROD image reference, source commit and registry digest, after checking that the Deployment is fully rolled out and each of its Pods is ready on that registry digest. On any failure it prints nothing on standard output and only the error type on standard error.

## Live drift report

`scripts/demo/live_drift.py` compares every object the Helmfile render would own with the live object of the same identity. The render is the six application releases of `helmfile.yaml` with `--include-crds` (the monitoring chart's ten CRDs and its twelve hooks included) and the four `demo-resources` companion releases of `helmfile-resources.yaml.gotmpl`: 220 application objects and 12 supplemental objects at the current pins. It renders with the image repository, tag and pull policy that the Jaeger and HotROD Deployments of both stacks run, read with the `--stack` checks above, because adoption must not change them. Every other render input comes from the checkout, so run it from a clean checkout of the commit under review.

Run it in Cloud Shell, first with the administrator kubeconfig. Once `ops/rbac/deployer.yaml` from the same commit is applied ([deployer access](deployer-access.md)), run it again as the deployer identity with that identity's kubeconfig and context. The deployer role reads every rendered identity: the objects in the four release namespaces, the named cluster-scoped objects, and the ten monitoring CRDs by name. Python 3 with PyYAML, `kubectl`, and public network access to the chart repositories and Docker Hub are required.

```bash
git clone https://github.com/jaegertracing/jaeger-demo.git && cd jaeger-demo
git checkout "$REVIEWED_COMMIT"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}" CONTEXT="$(kubectl config current-context)"
python3 scripts/demo/helmfile_tools.py run -- python3 scripts/demo/live_drift.py \
  --kubeconfig "$KUBECONFIG" --context "$CONTEXT" > live-drift-report.json
echo "exit status $?"
```

The live read makes only named `GET` requests through the read-only proxy of `live_observation.py`; it lists no collection except the Pods of the two Jaeger stacks that the image check follows. The render runs with an empty kubeconfig. The JSON report has a `summary` with the count of each class, `renderInputs` (the checkout revision and a SHA-256 of the image inputs), and one entry per identity:

- `class`: `identical` when the normalized live object equals the normalized render, `explained` when every difference has a reason code below, `unexplained` for any other difference, `missing` when the object is rendered but absent.
- `reasons`, and for an unexplained entry `unexplainedFields`: the field paths that differ, without values. For a Secret the path stops at `data`.
- `uidSHA256` (a SHA-256 of the UID), `renderSHA256` (of the normalized render), `liveSHA256` (the `live_observation.py --object` hash of the live object).
- For a Service, `serviceAllocationSHA256`: a SHA-256 over the UID, `clusterIP` and `clusterIPs`. Comparing it between the reports taken before and after adoption shows that adoption kept each Service and its addresses without publishing them.

The report never contains a field value, Secret data, a raw UID or an API error body. Exit status 0 means nothing is unexplained or missing, 1 means something is, and 2 means the report could not be produced; then nothing is printed on standard output and only the error type on standard error.

Normalization drops `status`, the server-managed metadata (`uid`, `resourceVersion`, `generation`, `creationTimestamp`, `managedFields`) and null values, sets each namespaced object's namespace to its release namespace, drops the namespace of a cluster-scoped object, and stores a Secret's `stringData` as base64 `data`. For a built-in kind, an empty or zero rendered field equals an absent live field; a custom resource keeps empty values, which can be meaningful there. Only these reason codes explain a difference:

| Code | Explains |
| --- | --- |
| `server-default` | A Kubernetes default for a field the render leaves unset or zero, with exactly the default value, from a fixed list per kind: workload, pod template, container, probe, volume, Service, Secret, binding subject and webhook defaults, the deprecated `serviceAccount` mirror of `serviceAccountName`, and the CRD conversion strategy `None` |
| `server-quantity` | A built-in resource quantity in the API server's canonical form, equal in value to the rendered one, such as `1000m` read back as `1` |
| `crd-schema-default` | A custom resource field the render leaves unset, with the default declared at that path in the rendered CRD schema |
| `helm-ownership` | The complete Helm ownership metadata (`app.kubernetes.io/managed-by`, `meta.helm.sh/release-name`, `meta.helm.sh/release-namespace`) of the expected release: the application release, or `demo-resources` for an allowlisted companion |
| `companion-unadopted` | An allowlisted companion with no Helm ownership metadata and no `helm.sh/resource-policy`, which the [ownership patch](helmfile-companions.md) adds at adoption |
| `kubectl-last-applied` | The `kubectl.kubernetes.io/last-applied-configuration` annotation on an allowlisted companion, which the deploy scripts create with `kubectl apply` |
| `service-allocation` | A Service's `spec.clusterIP`, `spec.clusterIPs`, `spec.ipFamilies` or `spec.ipFamilyPolicy` that the render leaves unset. A rendered value, such as `clusterIP: None` on a headless Service, must match exactly |
| `webhook-ca-bundle` | `caBundle` in the two `prometheus-kube-prometheus-admission` webhook configurations, which the admission patch hook writes |
| `deployment-revision` | The `deployment.kubernetes.io/revision` annotation that the Deployment controller writes |
| `daemonset-generation` | The `deprecated.daemonset.template.generation` annotation that the DaemonSet controller writes |
| `rollout-restart` | The `kubectl.kubernetes.io/restartedAt` pod template annotation on the two `trace-generator` Deployments, which the deploy scripts restart, and on `jaeger/jaeger`, which `opensearch_reset.py` restarts |
| `grafana-admin-password` | The `admin-password` of `default/prometheus-grafana`, which the chart keeps from the live Secret by `lookup` while the offline render uses the public fixture password, and the Grafana pod template's `checksum/secret` when it equals the chart's checksum of the live password |
| `hook-deleted` | A rendered Helm hook with the `hook-succeeded` delete policy that is absent, as Helm leaves it after a successful run |

Anything else is unexplained, including foreign, partial or missing Helm ownership, an unknown field, a selector, a finalizer or owner reference, a terminating object, and any ConfigMap or Secret content. A new reason needs a reviewed change to `REASONS` and its tests.

`scripts/demo/live_drift_test.py` runs the comparison without credentials against the sanitized fixture in `scripts/demo/testdata/live-drift`: `rendered.yaml` holds rendered objects by release, `live.yaml` holds live objects by identity (`null` for a 404) and `expected.yaml` holds each identity's class, reasons and unexplained fields. Every UID, address and secret in it is fake.

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
