#!/usr/bin/env bash
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
#
# Apply one reviewed plan printed by companion_ownership.py, for adoption or
# de-adoption. Every patch carries its own UID and resourceVersion tests, so a
# stale plan fails without a write. Stops at the first failure.
#
# Usage: apply-ownership-patches.sh PLAN.json KUBECONFIG CONTEXT

set -euo pipefail

if [[ $# -ne 3 || -z "$3" ]]; then
  echo "Usage: $0 PLAN.json KUBECONFIG CONTEXT" >&2
  exit 2
fi
plan=$1
kubeconfig=$2
context=$3

# Refuse anything other than guarded label and annotation changes on the four allowlisted kinds.
if ! jq -e '
    type == "array" and all(.[];
      (.namespace | type == "string") and (.name | type == "string") and
      (.kind | IN("ConfigMap", "Service", "Deployment", "Ingress")) and
      (.patch | type == "array") and
      all(.patch[];
        (.op == "test" and (.path | test("^/metadata/(uid|resourceVersion)$|^/metadata/(labels|annotations)/[^/]+$"))) or
        ((.op | IN("add", "remove")) and (.path | test("^/metadata/(labels|annotations)(/[^/]+)?$")))) and
      ((.patch | length) == 0 or
       ([.patch[] | select(.op == "test") | .path] | (index("/metadata/uid") != null and index("/metadata/resourceVersion") != null))))
  ' "$plan" >/dev/null; then
  echo "Unexpected patch plan: only guarded metadata patches on allowlisted kinds are applied" >&2
  exit 1
fi

count=0
while IFS= read -r item; do
  kubectl --kubeconfig "$kubeconfig" --context "$context" \
    --namespace "$(jq -r .namespace <<<"$item")" \
    patch "$(jq -r .kind <<<"$item")" "$(jq -r .name <<<"$item")" \
    --type=json --patch "$(jq -c .patch <<<"$item")" >/dev/null
  count=$((count + 1))
done < <(jq -c '.[] | select(.patch | length > 0)' "$plan")
echo "Applied ${count} metadata patches."
