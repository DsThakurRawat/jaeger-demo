# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

.PHONY: test lint

test:
	bash scripts/utils/run-tests.sh

lint:
	shellcheck scripts/demo/*.sh scripts/utils/*.sh examples/oci/*.sh examples/otel-demo/*.sh
	actionlint
	python3 -m json.tool renovate.json >/dev/null
