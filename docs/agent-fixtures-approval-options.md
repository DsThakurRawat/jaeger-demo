<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Agent-fixtures approval capacity: evidence and options

Discussion proposal for [#55](https://github.com/jaegertracing/jaeger-demo/issues/55),
not a selected fix. This document changes no values, limits, permissions,
admission policies or deployment behavior. The agent remains disabled in the
[checked-in values](../examples/otel-demo/otel-demo-values.yaml).

## Credential-free result

At configuration `f9b54fe60b039633d00b5ea91dba6a40722e8fad`, render the pinned
OpenTelemetry Demo chart `0.42.7` with the checked-in values and only
`components.agent.enabled=true` overridden. The archive SHA-256 is
`8641be206f5282f3c489a4fd8481d343039d1c21298e37feb6d324e6997a3f89`, matching the
[repository archive lock](../ops/executor/chart-archives.json).

Using Helm `4.3.2`, Kubernetes render version `1.37.0`, upgrade rendering,
release/namespace `otel-demo`, and the repository's
[`variant` and `canonical`](../scripts/demo/scheduled_approval_types.py):

| Measurement | Canonical bytes |
| --- | ---: |
| One `agent-fixtures` variant from the rendered ConfigMap | 827,964 |
| One variant after adding synthetic namespace and Helm ownership metadata | 828,065 |
| Current `MAX_PART` | 655,360 |

The second row is a local representation, not an observed live object. Metadata,
render flags and chart revisions can change exact counts. The stable finding is
that even the first, single-variant representation exceeds the cap. A second
copy alone would require at least 1,655,928 bytes before the partition envelope,
UID and other objects. This is not a measurement of the complete production
partition or a deployed Kubernetes/etcd object limit.

No cluster was contacted. These measurements do not establish a production
request share or error rate. The load-generator task weighting described in the
issue is not a measurement of production impact.

## Reproduce the single-variant measurement

Use the repository configuration named above for the recorded counts. Set
`CHART_ARCHIVE` to a local copy of the pinned archive, not an unpacked chart.
The following commands verify its digest before rendering. They create
`agent-fixtures-capacity-render.yaml` in the working directory; choose an empty
scratch working copy if that output name is already in use. Python needs the
repository's documented PyYAML dependency. The tool wrapper verifies its pinned
Helm binary; public network access may be needed for its cache.

```bash
(
set -euo pipefail
set -C
CHART_ARCHIVE=/absolute/path/to/opentelemetry-demo-0.42.7.tgz
python3 - "$CHART_ARCHIVE" <<'PY'
import hashlib
from pathlib import Path
import sys
expected = "8641be206f5282f3c489a4fd8481d343039d1c21298e37feb6d324e6997a3f89"
if hashlib.sha256(Path(sys.argv[1]).read_bytes()).hexdigest() != expected:
    raise SystemExit("Chart archive digest differs from the repository lock")
PY
KUBECONFIG=/dev/null python3 scripts/demo/helmfile_tools.py run -- \
  helm template otel-demo "$CHART_ARCHIVE" --namespace otel-demo \
  --kube-version 1.37.0 --is-upgrade \
  --values examples/otel-demo/otel-demo-values.yaml \
  --set components.agent.enabled=true > agent-fixtures-capacity-render.yaml
PYTHONPATH=scripts/demo python3 - <<'PY'
from pathlib import Path
import yaml
import scheduled_approval_types as t
objects = yaml.safe_load_all(Path("agent-fixtures-capacity-render.yaml").read_text())
fixtures = [o for o in objects if o and o.get("kind") == "ConfigMap"
            and o["metadata"]["name"] == "agent-fixtures"]
assert len(fixtures) == 1
size = len(t.canonical(t.variant(fixtures[0])))
print({"singleVariantBytes": size, "MAX_PART": t.MAX_PART})
assert size > t.MAX_PART
PY
)
```

This measures a necessary lower bound, not six-chart acceptance. An eventual fix
must still pass the complete credential-free capacity rehearsal and the native
admission/rollback checks required by its chosen design. Existing full rehearsal
entry points are
[`all_five_candidates.py`](../scripts/demo/rehearsals/helm-history/all_five_candidates.py)
and [`six_chart_approvals.py`](../scripts/demo/rehearsals/helm-history/six_chart_approvals.py);
the latter requires a Kubernetes test environment and is not run by this recipe.

## Why repartitioning alone is insufficient

[`partition`](../scripts/demo/scheduled_approval_types.py) currently hashes
`default`-namespace objects into buckets and assigns other namespaces, including
`otel-demo`, one partition each. More buckets could separate objects, but cannot
make this one variant fit under the current per-partition cap.

The [preparer](../scripts/demo/scheduled_approvals.py) stores the old and intended
object variants with their UID and checks the canonical partition size. The
[capacity rehearsal](../scripts/demo/rehearsals/helm-history/all_five_candidates.py)
also models three comparable variants for rollback capacity. The
[native policy generator](../ops/executor/approval_policies.py) binds each target
to a named parameter and compares old/new metadata and complete body fields
against approved variants, with the existing explicit volatile-field rules.

Therefore compressing only the Python payload is not a drop-in storage change:
the schema and CEL comparisons currently expect the expanded object fields.
Splitting one object's approval across parameters or replacing its body with a
digest likewise requires a reviewed protocol and enforcement design. Adding the
ConfigMap to an exclusion without a replacement would remove its present
full-body approval coverage. Increasing the cap is not shown safe by this
single-object measurement and would need full-envelope and API-limit evidence.

## Alternatives for maintainer discussion

| Direction | What would need to be established | Tradeoff / unresolved gate |
| --- | --- | --- |
| Keep the agent off and record the decision | Measure actual failed requests/spans over a stated interval, document the accepted impact and owner decision | Does not restore agent functionality; task weighting alone cannot satisfy the impact measurement |
| Change fixture delivery in the demo/chart | Agree an upstream-compatible delivery model, reproducibility and image/content provenance; render and test agent behavior | Avoids assuming an approval-protocol exception, but changes fixture delivery and needs upstream coordination |
| Give this named object a separate reviewed apply/enforcement path | Specify exact identity/content authorization, role boundaries, rollback and compensating enforcement before exclusion | A plain kind-wide or named exclusion is not that design and is not proposed for deployment here |
| Redesign approval storage/enforcement | Define complete old/new/rollback authorization, partition/envelope bounds, admission verification, failure atomicity and audit evidence | Compression, chunks or digests change the protocol; no such implementation is selected or validated here |

## Required decision and evidence

Maintainers/operators still need to choose a direction and acceptable impact.
For an agent-on implementation, preserve authorization of exact intended content
and UID, existing receipt/slot binding, fail-closed behavior and rollback
coverage, and demonstrate the full six-chart capacity and native admission
checks. Test changed fixture content, missing/foreign parameters and incomplete
publication as applicable to the selected design. Do not weaken these checks
just to make the oversized object fit.

For an agent-off decision, record the measured production impact and rationale
specified by #55. No option is selected here, no production rate is asserted,
and this proposal does not close the issue.
