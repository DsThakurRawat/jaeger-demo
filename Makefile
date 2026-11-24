# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

.PHONY: test lint renovate-discovery check helmfile-tools validate validate-manifests

RUFF ?= ruff

test:
	bash scripts/utils/run-tests.sh

lint:
	shellcheck scripts/demo/*.sh scripts/utils/*.sh examples/oci/*.sh examples/otel-demo/*.sh ops/oke/*.sh
	actionlint
	python3 -m json.tool renovate.json >/dev/null
	$(RUFF) check .

renovate-discovery:
	bash scripts/utils/renovate-discovery.sh

check: lint test renovate-discovery

helmfile-tools:
	python3 scripts/demo/helmfile_tools.py check

validate-manifests:
	python3 scripts/demo/helmfile_tools.py run -- python3 scripts/demo/validate_helmfile.py $(MANIFESTS)

validate: lint
	python3 scripts/demo/helmfile_tools.py run -- bash scripts/demo/validate-helmfile.sh
