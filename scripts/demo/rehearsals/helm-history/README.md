<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Helm history and rollback rehearsal

A fresh, local Kubernetes 1.36.0 API server and etcd 3.6.8 enforced the six-release Secret policy. The rehearsal used Helm 4.3.2. Every release pruned revision 1 after three upgrades with `--history-max 3`. The next upgrade attempted to change an immutable ConfigMap. Kubernetes rejected that change, Helm marked revision 5 failed, then recorded an automatic rollback as deployed revision 6. Each ConfigMap kept its UID and original data. The four retained revisions after rollback were 3, 4, 5 and 6. These fixture revision numbers are independent of production.

| Namespace | Fixture release names |
| --- | --- |
| `default` | `jaeger`, `prometheus` |
| `jaeger` | `jaeger` |
| `opensearch` | `opensearch`, `opensearch-dashboards` |
| `otel-demo` | `otel-demo` |

All six transactions used the restricted fixture identity. Eleven negative tests exercised admission and RBAC. Nine tested real Secret admission denials, including CREATE with a mismatched type, release or revision, UPDATE or DELETE of another application’s Secret, and attempted adoption of an existing record. RBAC denied namespace access outside the four Roles and changes to the admission policy. The fixture writer could read unrelated Secrets in its release namespaces, as expected with Helm Secret storage. [Receipt](six-release-helm-history.json)

## Reproduce

Obtain the `kube-apiserver`, `etcd` and `kubectl` binaries from the official Kubernetes controller-tools envtest release matching your platform. Verify the archive digest published with the release before extracting the binaries into an isolated directory. On the tested Darwin arm64 platform the 1.36.0 archive SHA-256 was `211e620e9f61085ac2e3a176a4f4fc5ebc60d40be1dae9ab5e35895f0c748700`. Install Helm 4.3.2, Python 3 and OpenSSL with support for `req -addext`. [Official envtest release](https://github.com/kubernetes-sigs/controller-tools/releases/tag/envtest-v1.36.0)

For example, to inspect the expected archive SHA-256 recorded by the release publisher, set the operating system and architecture to match the archive you downloaded:

```bash
archive=envtest-v1.36.0-darwin-arm64.tar.gz
gh api repos/kubernetes-sigs/controller-tools/releases/tags/envtest-v1.36.0 |
  jq -er --arg archive "$archive" '.assets[] | select(.name == $archive) | .digest'
shasum -a 256 "$archive"
```

Compare the two digests before extracting. Place the [script](rehearse-six-release-history.py) next to the [policy fixture](helm-history-policy.yaml). Supply the directory containing the three executable binaries.

```bash
python3 rehearse-six-release-history.py \
  --assets /path/to/envtest/bin \
  --receipt ./six-release-helm-history.json
```

The script checks supported binary versions and rejects unrelated receipt outputs. It launches the API server and etcd on ephemeral IPv4 loopback ports and creates a temporary local administrator credential. Every Helm and kubectl operation selects the fixture’s explicit kubeconfig and context. Application operations impersonate the dedicated restricted identity. The policy requires both old and new identities on UPDATE and the stored identity on DELETE. On exit both processes stop and the temporary credentials, etcd database and synthetic release histories are removed. The receipt contains no kubeconfig or Secret values.

## Actual OpenSearch charts

The [storage chart rehearsal](storage-chart-rehearsal.md) extends this isolated API with both pinned charts, reviewed workload changes, content enforcement and a seeded PVC binding. Use the separate `--storage-charts` option to include it.

## Scheduled adapter rehearsal

Render all six application packages with the existing public script inputs.
Place the four packages listed in `docs/all-five-chart-access.json` in one
directory and prepare the pinned Jaeger Git chart checkout, including its
packaged dependencies. Run the following command from the repository root.

```sh
python3 scripts/demo/rehearsals/helm-history/all_five_candidates.py \
  --charts "$CHARTS" --jaeger-checkout "$JAEGER_CHART_CHECKOUT"
```

The fixture compares old tag-only snapshots with new digest references, checks
the exact named resource inventory and compares protected manifests. The six
charts contain 212 Helm transaction objects. The scripts also handle 12
supplemental objects. Monitoring's ten static CRDs are checked separately
against the embedded hook archive by the live adapter.
[Offline six-chart receipt](all-five-candidates.json)

Add `--frozen-transaction` to the private history rehearsal to run the signed
post-renderer with a restricted writer. Add `--monitoring-lookup /path/to/kube-prometheus-stack-82.10.4.tgz`
to seed the chart CRDs and a synthetic Grafana Secret, then exercise its actual
server-side dry-run using a read-only local identity.

## Production boundary

All six fixture releases used the same synthetic chart. The frozen production scripts use automatic rollback for the two Jaeger releases and require operator inspection for the four other releases. The local test proves the six release identities can write, prune and roll back Helm history through this envelope. It checks neither the content of encoded Helm payloads nor full production chart operation. Monitoring application Secrets and hooks, real workloads, kube-system Services, the live PVC binding, the OKE identity, public health and the unattended GitHub job still require their own verification. A separately authorized manual OpenSearch backup/restore needs its own access and capacity assessment. Scope a production policy to the actual scheduled writer and include the separately reviewed chart-owned Secret handling. The fixture policy uses an inert test principal.
