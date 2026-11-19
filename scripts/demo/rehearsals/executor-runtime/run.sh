#!/usr/bin/env bash
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail

: "${EXERCISE_IMAGE:?Set EXERCISE_IMAGE to the local executor image by digest}"
fixture_dir=$(cd "$(dirname "$0")" && pwd)
umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
kind get kubeconfig --name jaeger-executor-ci > "$work/kubeconfig"
python3 "$fixture_dir/probe_workload.py" --kubeconfig "$work/kubeconfig" --work "$work" --image "$EXERCISE_IMAGE" "$@"
