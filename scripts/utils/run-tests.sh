#!/usr/bin/env bash

# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
: "${SHUNIT2:?Set SHUNIT2 to a directory containing shunit2}"
if [[ ! -f "$SHUNIT2/shunit2" ]]; then
  echo "shunit2 not found at $SHUNIT2/shunit2" >&2
  exit 1
fi
SHUNIT2="$(cd "$SHUNIT2" && pwd)"
export SHUNIT2
cd "$ROOT"
python3 -m unittest discover -s scripts/demo -p '*_test.py' -v
for test_file in \
  scripts/utils/resolve-demo-snapshot-tags.test.sh \
  examples/otel-demo/deploy-all.test.sh \
  examples/otel-demo/jaeger-values.test.sh \
  examples/otel-demo/opensearch-expand-volume.test.sh \
  examples/otel-demo/opensearch-recovery.test.sh; do
  bash "$test_file"
done
