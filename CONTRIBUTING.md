<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Contributing

Open an issue describing the requested change before a PR. Keep one issue per PR,
update documentation alongside behavior, and retain existing copyright/license
headers. New files use the current-year Jaeger copyright and Apache-2.0 SPDX
identifier. Maintainers review changes under
[CODEOWNERS](.github/CODEOWNERS) and the organization
[AI Usage Policy](AI_POLICY.md).

Sign each new commit with `git commit -s`. Capitalize the first word after a
conventional prefix, for example `fix(deploy): Resolve the core image commit`.
The extraction PR must use a history-preserving merge; see
[extraction.md](docs/extraction.md). Do not rewrite imported authored commits to
make them look newly contributed.

## Local checks

Install Bash, Python 3 with PyYAML, Git, jq, Helm, ShellCheck, actionlint and ruff
(`python3 -m pip install -r scripts/utils/lint-requirements.txt`). Keep production
kubeconfig and cloud credentials out of the test environment. Obtain shunit2:

```bash
git clone --branch v2.1.8 --depth 1 https://github.com/kward/shunit2.git .tools/shunit2
export SHUNIT2="$PWD/.tools/shunit2"
make lint
make test
```

`make check` runs `lint`, `test` and `renovate-discovery` in that order.

The tests mock deployment commands and render pinned public charts. Public network
access is required for the chart repositories, but no production credentials are
used. The same shell/Python suites run in the `Demo tests` CI check. New behavior
needs tests; bugs need regression coverage. Preserve imported test assertions.

`make lint` runs ShellCheck for shared and example scripts, actionlint for workflows, JSON
syntax validation, and ruff for Python with the rules in `ruff.toml`. The script
handover uses the production writer's Helm version. The toolchain and schema
checks for Helmfile adoption are documented below.

## Renovate discovery

The `Renovate discovery` CI check validates configuration and verifies actual
extraction of every hosted dependency location. With Docker available, reproduce it
without credentials:

```bash
make renovate-discovery
```

The script runs the configuration validator, the extraction, and the checker. Set
`CONTAINER_ENGINE=podman` to use Podman instead of Docker. Renovate considers hosted updates daily, waits
seven days, and never automerges them. Preserve paired OpenSearch/Dashboards 3.x
updates and the PostgreSQL/Valkey major constraints. Do not duplicate dependency
bumps already managed by automation.

## Production changes

Tests and PRs require no production access. Keep production workflows main-only. Scheduled deployment requires the
[activation and verification procedure](docs/cutover.md#review-scheduled-scope). Repository
protection and DCO/Renovate app coverage must be verified, not inferred from files.

The first access transition, script handover, Helmfile adoption and final
acceptance require human verification of concrete inputs and evidence. Subsequent
routine operations follow the accepted [release process](docs/releases.md).
Never place credentials, raw secrets or unreviewed live configuration in a commit,
issue, PR or test fixture.

## Helmfile adoption tooling

Local commands and the credential-free CI check use the same [toolchain lock](scripts/demo/helmfile-tools.lock.json).
It pins Helm 4.3.2, the major and version recorded by the deployment writer, plus
Helmfile, helm-diff and kubeconform. Linux and macOS on amd64 and arm64 are supported.
Python 3, curl and PyYAML are required. No Kubernetes or Oracle credentials are needed
to verify tools or validate a local render.

```bash
make helmfile-tools
python3 scripts/demo/helmfile_tools.py run -- helmfile --version
make validate-manifests MANIFESTS='rendered-applications.yaml rendered-companions.yaml'
```

The wrapper downloads official release archives into the ignored `.tools/helmfile-archives`
cache, checks SHA-256 on every use, and extracts the pinned executable and diff plugin
into a temporary directory for each command. It selects that plugin explicitly. The
`run` subcommand inherits the caller's environment, including any configured kubeconfig.
Use credential-free inputs for local render and validation. The cache includes the
hash-verified Kubernetes OpenAPI specification when running schema validation.

Pass complete YAML streams with the monitoring chart's CRDs included. The validator
uses the upstream Kubernetes v1.36.1 OpenAPI definitions and the exact served CRD
schemas in those streams. It checks exact API group, version and kind, applies
strict field and type checks, and fails on missing schemas. Native optional fields
accept Kubernetes' null-as-omitted representation. Server-side defaulting, CEL and
webhook behavior are tested against Kubernetes during adoption rehearsals.
The credential-free rehearsal rendered 222 application objects plus 12 supplemental
objects and validated all 234, including ten CRDs, with zero skips.

Renovate discovers all four tool releases and opens reviewable updates. For a version
change, refresh that tool's checksums from its official release and review the lock diff:

```bash
python3 scripts/demo/helmfile_tools.py refresh helmfile
make helmfile-tools
```

The SHA-256 keys include the version, so changing a version alone fails verification.
For a Kubernetes version update, select the production API version and review the
SHA-256 of `api/openapi-spec/swagger.json` from that tagged release. Verify the
combined render and include the changes to versions, checksums and validation results
in the release record.
