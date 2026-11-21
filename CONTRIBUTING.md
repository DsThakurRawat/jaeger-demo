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
syntax validation, and ruff for Python with the rules in `ruff.toml`. Imported
application configuration is kept unchanged during extraction. Toolchain
centralization and Helmfile render/schema checks belong to the next
implementation stage. Production Helm's effective version must be frozen after
establishing the last release writer's major before handover.

## Dependency pins with a second location

Renovate updates the first location of a pin only. Each second location below fails
a test with the command or files to change, so a bump PR cannot pass CI and fail at
preparation.

- **Chart versions.** After a chart pin changes in either `deploy-all.sh`, run
  `python3 scripts/utils/update-chart-archives.py` and commit the resulting
  `ops/executor/chart-archives.json`. It downloads each of the four pinned archives,
  requires its SHA-256 to equal the digest in the publisher's `index.yaml`, and
  rewrites the `charts` map only. `--check` changes nothing and exits non-zero with
  the differences. Executor handling of these files is described in
  [in-cluster-executor.md](docs/in-cluster-executor.md).
- **Helm and Kubernetes versions.** `scripts/demo/version_literals_test.py` reads
  the versions in `scripts/demo/scheduled_adapter.py` and lists every other literal
  that disagrees. Change all listed files in one commit.
- **OCI CLI.** `scripts/demo/oci-cli-constraints.txt` records what the deployment
  runner installed, including packages already present, not only new ones (see its
  header). To regenerate it, take the full `pip freeze` output of the runner that
  performed the deployment after `oci-cli` was installed, keep the license header and
  provenance comment with the new run URL, and replace the pins. Update the literal
  assertions in `scripts/demo/verify_oci_cli_test.py` (`oci-cli`, `oci` and
  `python-dateutil`) in the same commit.

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
