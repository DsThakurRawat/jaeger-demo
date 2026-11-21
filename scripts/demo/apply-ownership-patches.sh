#!/usr/bin/env bash
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
#
# Apply one reviewed plan printed by companion_ownership.py, for adoption or
# de-adoption. Every patch carries its own UID and resourceVersion tests, so a
# stale plan fails without a write. Stops at the first failure.
#
# For adoption, pass RECORD.json. Each patch that succeeds is appended to it, so
# after a partial failure and a retry with a fresh plan it still holds exactly
# what was added to every object. De-adoption takes this record as its input.
#
# Usage: apply-ownership-patches.sh PLAN.json KUBECONFIG CONTEXT [RECORD.json]

set -euo pipefail

if [[ $# -lt 3 || $# -gt 4 || -z "$3" ]]; then
  echo "Usage: $0 PLAN.json KUBECONFIG CONTEXT [RECORD.json]" >&2
  exit 2
fi
plan=$1
kubeconfig=$2
context=$3
record=${4:-}

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

if [[ -n "$record" ]]; then
  [[ -e "$record" ]] || echo '[]' > "$record"
  # One entry per object, written by the plan that adopted it. A second entry would
  # hide what the first one added.
  if ! jq -e --slurpfile recorded "$record" '
      ($recorded | length == 1) and ($recorded[0] | type == "array") and
      ([.[] | select(.patch | length > 0) | [.namespace, .kind, .name]] as $new
       | [$recorded[0][] | [.namespace, .kind, .name]] as $old
       | ($new - $old) == $new)
    ' "$plan" >/dev/null; then
    echo "The record is not a list, or already holds an object that this plan would patch" >&2
    exit 1
  fi
fi

count=0
while IFS= read -r item; do
  kubectl --kubeconfig "$kubeconfig" --context "$context" \
    --namespace "$(jq -r .namespace <<<"$item")" \
    patch "$(jq -r .kind <<<"$item")" "$(jq -r .name <<<"$item")" \
    --type=json --patch "$(jq -c .patch <<<"$item")" >/dev/null
  if [[ -n "$record" ]]; then
    jq --argjson item "$item" '. + [$item]' "$record" > "$record.tmp"
    mv "$record.tmp" "$record"
  fi
  count=$((count + 1))
done < <(jq -c '.[] | select(.patch | length > 0)' "$plan")
echo "Applied ${count} metadata patches."
