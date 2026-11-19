<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Supplemental resource adoption

The local `demo-resources` chart defines one release in each of the four existing namespaces. The chart reads its allowlist from its own package. A render fails if a resource is missing, duplicated, of the wrong API version, outside the namespace, or outside the allowlist. Every rendered object carries the Helm keep annotation. The chart takes the same Ingress, Service, Deployment and generator inputs as the extracted scripts. A ConfigMap embeds the exact script and requirements bytes for each generator. [Allowlist](../charts/demo-resources/allowlist.yaml)

## Render the public sources

Run these commands from the repository root with Python, PyYAML and the locked adoption tools installed. Write the generated inputs to a private temporary directory. The companion state is separate from the six application releases so operators can review the ownership transition independently.

```bash
work=$(mktemp -d)
python3 scripts/demo/companion_values.py > "$work/companions.json"
python3 scripts/demo/helmfile_tools.py run -- helmfile -f helmfile-resources.yaml.gotmpl \
  --state-values-file "$work/companions.json" template --skip-deps > "$work/companions.yaml"
```

## Inspect existing ownership

Use the read-only plan kubeconfig for the approved cluster context. The planner performs twelve named GETs. It rejects controller-owned objects, old or partial Helm ownership, conflicting retention, missing identities and deletion in progress. An already owned object returns an empty patch. Proposed changes modify top-level metadata only. The UID and resource version in each JSON Patch protect against replacement or concurrent change. A stale patch requires a fresh check of the object and its proposed inputs.

```bash
python3 scripts/demo/companion_ownership.py --kubeconfig "$PLAN_KUBECONFIG" \
  --context "$PLAN_CONTEXT" > "$work/ownership.json"
```

This ownership plan is one input to the reviewed deployment bundle. The production decision also requires full configuration and live-state comparison, including chart inputs, Services, pod templates and rollback state. Keep raw production inventory and release data in private storage.

## Rehearse in a fresh local cluster

The test requires a fresh loopback kind cluster. It seeds all twelve objects as kubectl-managed resources and restarts both generators, as the extracted scripts do. It checks foreign ownership rejection, runs the planner with named GET permissions, applies metadata patches, then runs Helmfile twice. It compares each complete spec, ConfigMap payload and UID across adoption. The second apply must preserve every Helm revision. A final chart update drops one Service and verifies that its UID and spec survive.

```bash
kind create cluster --name jaeger-demo-adoption-fixture --kubeconfig "$work/kind.yaml" \
  --image kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5
python3 scripts/demo/helmfile_tools.py run -- python3 scripts/demo/rehearsals/companion-adoption/exercise.py \
  --kubeconfig "$work/kind.yaml" --context kind-jaeger-demo-adoption-fixture
kind delete cluster --name jaeger-demo-adoption-fixture --kubeconfig "$work/kind.yaml"
```

The same test can use an explicitly isolated envtest API server with context `fixture` to check Kubernetes defaults, Helm behavior and ownership. Envtest runs the API server and etcd. The kind rehearsal additionally runs workload controllers and Pods. [kind 0.32.0 and its Kubernetes 1.36.1 node image](https://github.com/kubernetes-sigs/kind/releases/tag/v0.32.0)
