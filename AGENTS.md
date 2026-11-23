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

## Keeping documentation current

Documentation is part of the change, not a follow-up. In the same PR, update
every document the change makes stale:

| When the PR changes | Update |
| --- | --- |
| Behavior, commands, workflow inputs, alerts or failure handling | The README or runbook that documents it; [docs/releases.md](docs/releases.md) for release procedure |
| Public routes, namespaces, releases, components, trace flow, workflows or deployment steps | Every Mermaid diagram that shows them |
| Anything an operator or contributor would notice | [CHANGELOG.md](CHANGELOG.md), following [CONTRIBUTING.md](CONTRIBUTING.md#changelog) |
| A migration phase's status or evidence | [docs/MIGRATION.md](docs/MIGRATION.md), without marking future work done |
| Local toolchain, check commands or a pin with a second location | [CONTRIBUTING.md](CONTRIBUTING.md) |

- Before opening or updating a PR, search the Markdown files for each identifier
  the change renames, removes or redefines (file paths, flags, environment
  variables, inputs, release and workflow names) and fix every stale reference.
- Verify documentation against the repository files, not against memory or other
  documents. Each diagram node and edge must trace to a file.
- A merge is not a deployment. Never record production state in the changelog,
  README or diagrams; that evidence belongs in the release record and migration
  plan.
- Stale documentation found outside the PR's scope gets its own issue rather than
  a wider PR.
- Run `python3 scripts/demo/changelog_check.py origin/main HEAD` before pushing.
  Never apply the `skip-changelog` label yourself; when you think a change needs
  no entry, say why and let the human decide.
