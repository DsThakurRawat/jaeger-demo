#!/usr/bin/env bash
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
export KUBECONFIG=/dev/null
export HELM_CACHE_HOME="$root/.tools/validation/cache"
export HELM_CONFIG_HOME="$root/.tools/validation/config"
export HELM_DATA_HOME="$root/.tools/validation/data"
mkdir -p .tools
# shellcheck source=./scripts/demo/jaeger-chart.sh
source scripts/demo/jaeger-chart.sh
prepare_jaeger_chart "$root/.tools/helm-charts"
# The script-era reference renders use these repository aliases. The Helmfile side
# renders from .helmfile-locked.yaml, which equivalence.py derives from the chart lock.
helmfile -f helmfile.yaml repos
python3 scripts/demo/rehearsals/helmfile-applications/equivalence.py --validate

# No real render has a CRD that restricts metadata. Check that handling against kubeconform itself.
check=$(mktemp -d)
trap 'rm -rf "$check"' EXIT
cat > "$check/crd.yaml" <<'YAML'
apiVersion: apiextensions.k8s.io/v1
kind: CustomResourceDefinition
metadata: {name: alarms.example.com}
spec:
  group: example.com
  scope: Namespaced
  names: {kind: Alarm, plural: alarms}
  versions:
  - name: v1
    served: true
    storage: true
    schema:
      openAPIV3Schema:
        type: object
        properties:
          metadata:
            type: object
            properties:
              name: {type: string, pattern: ^allowed-}
          spec: {type: object, properties: {level: {type: string}}}
YAML
for name in allowed-one wrong; do
  { cat "$check/crd.yaml"
    printf -- '---\napiVersion: example.com/v1\nkind: Alarm\nmetadata: {name: %s, namespace: default, labels: {team: demo}}\nspec: {level: high}\n' "$name"
  } > "$check/$name.yaml"
done
python3 scripts/demo/validate_helmfile.py "$check/allowed-one.yaml" >/dev/null
if python3 scripts/demo/validate_helmfile.py "$check/wrong.yaml" >/dev/null 2>&1; then
  echo "Schema validation accepted a name that violates the CRD's metadata pattern" >&2
  exit 1
fi
echo "CRD metadata restriction enforced beside ObjectMeta."
