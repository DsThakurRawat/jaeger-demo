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
helmfile -f helmfile.yaml repos
python3 scripts/demo/rehearsals/helmfile-applications/equivalence.py --validate
