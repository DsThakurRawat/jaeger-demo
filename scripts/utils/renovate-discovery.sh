#!/usr/bin/env bash

# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

# Validate the Renovate configuration and verify, without credentials, that
# Renovate extracts every hosted demo dependency location. Set CONTAINER_ENGINE
# (default docker) to use another compatible engine such as podman.

set -euo pipefail

engine="${CONTAINER_ENGINE:-docker}"
image="ghcr.io/renovatebot/renovate:latest"

extraction="$(mktemp)"
trap 'rm -f "${extraction}"' EXIT

"${engine}" run --rm -v "${PWD}/renovate.json:/usr/src/app/renovate.json" \
  "${image}" renovate-config-validator

"${engine}" run --rm -v "${PWD}:/repo" -w /repo \
  -e LOG_LEVEL=debug -e LOG_FORMAT=json \
  -e RENOVATE_PLATFORM=local -e RENOVATE_DRY_RUN=extract \
  -e RENOVATE_ONBOARDING=false -e RENOVATE_REQUIRE_CONFIG=required \
  -e RENOVATE_ENABLED_MANAGERS=custom.regex,kubernetes,helm-values,pip_requirements,helmfile \
  -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=safe.directory -e GIT_CONFIG_VALUE_0=/repo \
  "${image}" renovate > "${extraction}"

python3 scripts/demo/check-renovate-extraction.py "${extraction}"
