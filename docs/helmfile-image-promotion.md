<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Snapshot image promotions

Initial Helmfile adoption preserves the live image references, including tag-only syntax. A separate promotion selects immutable candidates and checks the Jaeger binary against both chart-rendered configurations. This preparation is credential-free and performs no cluster operations.

## Resolve public snapshots

```bash
mkdir -p .tools/promotion
python3 scripts/demo/promotion_images.py resolve --event schedule \
  --output .tools/promotion/image-selection.json
```

Use a new output path per candidate. The resolver reads the configuration checkout's commit, resolves `jaegertracing/jaeger` main directly from Git and passes that core SHA to the shared tag resolver. It checks Jaeger and HotROD in their separate Docker Hub repositories. Only a scheduled selection with an unspecified image override can fall back from an unpublished core SHA to a published SHA tag. An explicit `--jaeger-tag` or `--hotrod-tag` must exist. The command itself refuses a selection whose resolved tag differs from an explicit tag, so this holds whichever fallback the shared resolver applies. An explicit `latest` must resolve to a published SHA tag. A registry or network error aborts selection.

The selection records the independent source commits, Docker registry digests, Linux AMD64 manifests and exact deployment references. The resolver hashes the received manifest and configuration bytes, verifies the registry content digest and confirms the node platform. A named tag must be traceable to a matching full SHA tag with the same digest. The `images` field is the Helmfile state-values input for both stacks.

## Validate the candidate binary

Render both Jaeger releases from the reviewed checkout with those exact image inputs. Use the pinned tools and prepared chart described in the application release documentation. A typical local rehearsal is:

```bash
set -eu
export KUBECONFIG=/dev/null
selection=.tools/promotion/image-selection.json
selection_sha=$(shasum -a 256 "$selection" | awk '{print $1}')
for stack in oci otel; do
  helmfile -f helmfile.yaml --state-values-file "$selection" --skip-deps \
    -l "stack=$stack,component=jaeger" template --skip-needs --kube-version 1.36.1 \
    > ".tools/promotion/$stack-jaeger.yaml"
done
python3 scripts/demo/promotion_images.py validate --selection "$selection" \
  --selection-sha256 "$selection_sha" \
  --oci-render .tools/promotion/oci-jaeger.yaml --otel-render .tools/promotion/otel-jaeger.yaml \
  --runtime docker --output .tools/promotion/image-compatibility.json
```

Use `--runtime podman` with Podman. The runtime needs Linux AMD64 execution and access to bind-mount the temporary config directory. A remote Podman VM needs a shared `TMPDIR` at the same absolute path. The resolver and runtime pull public registry data. The Jaeger validation containers use an isolated network, read-only root and config mounts, user 10001 and dropped capabilities.

The validator checks the selection digest and config commit, resolves both pinned registry manifests by digest, and verifies the two rendered Deployment image references. It follows the Jaeger container's ConfigMap mounts, preserves the rendered environment and runs the selected binary's `validate --config /etc/jaeger/user-config.yaml` for each stack. Both commands must exit successfully. The output records the selection digest, config commit, both rendered-manifest hashes, effective config and UI hashes, validated platform manifest and image provenance.

Attach the original selection and compatibility receipt to the promotion plan. The rendered inputs used here must be the same bytes the bundle contains. An incompatible candidate leaves the prior deployment and its last-known-good references intact. Record the failing stack and image in the incident or issue before retrying with a corrected candidate. The local validator checks configuration compatibility; runtime ingestion, query, UI and SPM checks are still part of deployment acceptance.
