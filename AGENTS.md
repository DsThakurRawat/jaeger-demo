<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Agent guidance

Read [CONTRIBUTING.md](CONTRIBUTING.md), [AI_POLICY.md](AI_POLICY.md), and the current
[migration plan](docs/MIGRATION.md) before work. The human contributor owns agent
output. Human approval is required for each proposed reply to review feedback.

- Require an existing open issue, keep one issue per PR, and stay in its scope.
- Use `codex/` for agent branches. DCO-sign new commits with `git commit -s` and
  capitalize the description after `type(scope):`.
- Run `make lint`, `make test`, and Renovate validation/discovery for affected
  dependency configuration before opening or updating a PR. Report limitations.
- Add regression tests for changed behavior. Do not weaken assertions or bypass
  checks. Preserve imported authored history and license headers.
- Keep extraction adapters minimal. Helmfile, recovery removal and post-adoption
  refactors are separate stages with explicit prerequisites.
- Do not treat local test success as production acceptance. Follow the concrete
  access/cutover/adoption/acceptance gates and bind verification to exact inputs.
- Preserve the cluster, DNS/routes, namespaces, release identities, Service
  addresses and PVC bindings. Certificate issuers are externally owned.
- Routine implementation, tests, read-only diagnostics and accepted operations can
  proceed autonomously. Ask one focused question when access or a consequential
  unresolved decision blocks progress. Do independent work while waiting.
- Update commands, release procedures and operational documentation in the same
  PR as behavior changes. Record phase evidence without marking future work done.
