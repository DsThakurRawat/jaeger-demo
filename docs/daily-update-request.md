<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Daily deployment requests

The daily workflow uses the `demo-scheduled` credential to read and patch one ConfigMap, `jaeger-demo-operations/daily-hosted-update`. GitHub holds its deployment concurrency group while the request is outstanding. Its failure step sends the existing Slack alert when the request, receiver or verification fails. The schedule switch defaults to off. The independent production planner, bundle storage and executor must be present and rehearsed before enabling it. [Workflow](../.github/workflows/ci-deploy-demo.yml) · [Protocol implementation](../scripts/demo/scheduled_request.py)

| Actor | Responsibility |
| --- | --- |
| Trusted preparer | Follow the approved configuration and release policy each day. Resolve upstream Jaeger and HotROD independently, freeze the five-component selection, exact images and charts, required live preconditions and verification inputs in a private immutable bundle. Store it by digest. Publish its `configurationSHA` and `bundleSHA256` for the UTC slot. |
| GitHub requester | Read the original GitHub run creation time and the matching 13:00 UTC request. Verify the date, configuration commit, engine and expiry. Signal it once and observe the terminal result. The credential has no Helm, workload or Secret permissions through the request role. |
| Protected executor | Authenticate as an independently bound principal. Obtain the authorized receipt from trusted custody, validate the fixed plan, claim the shared writer lock, authenticate/decrypt the bundle and check live drift before any mutation. Apply its frozen inputs and perform its recorded functional verification. |
| Recovery operator | Use a fourth authenticated identity to reconcile a failed or verified operation after inspecting and confirming the stopped owner and durable outcome. |
| Manual operator | Run the existing reviewer-gated jobs. All three acquire the same durable lock once the protocol is provisioned. A failed or interrupted writer requires inspection. |

The control record contains the schema, engine, UTC slot and expiry, the public two-field bundle receipt, state and signal time. The receiver accepts a signal only within five minutes of its timestamp and before the prepared expiry. The default preparer creates a two-hour admission window, and the protocol bounds any window to six hours. A rerun uses the original GitHub creation date. One signal can result in at most one receiver claim. While the record still names that slot, a late rerun observes its in-flight operation or reports the recorded result after expiry. After the preparer advances the record to a later date, consult the original run and durable receipt for the older result. Expired unclaimed requests still fail. [Admission and bindings](../scripts/demo/rehearsals/scheduled-request/access.py)

The signed bundle preparer and receiver run under distinct in-cluster service accounts. [Installation, approval gates and operations](in-cluster-executor.md)

## Set up once

1. Review the four distinct authenticated Kubernetes usernames, independent runner identities and release custody. The scheduled job needs only its existing OCI cluster connection and the named ConfigMap role. The preparer and executor must run outside the requester's process and credential environment. Restrict private bundle access, retention and decryption to the independently authorized infrastructure.
2. Render the ConfigMap, role, binding and native admission policy for operator review. The manifest grants request-record access only. The recovery operator separately needs named `get` and `patch` on the existing deployment checkpoint. Before granting that access, confirm the recovery guards are installed on all serving API endpoints. Under that identity, test a server-side dry run of an unrelated checkpoint change and confirm the named recovery policy denies it. The recovery lock policy reads the request as a parameter. The recovery result policy reads the checkpoint and requires an unlocked record. The executor needs its own separately reviewed read, write, rollback and verification permissions for the approved bundles. Bootstrap the empty control record once. Install the guard and verify that a server-side dry run is denied by the named policy before granting the role binding. The independent bootstrap user must be authorized to patch the object and must differ from the four bound usernames. Repeat the effective checks across the cluster’s API endpoints before activation. Apply no policy changes from the scheduled workflow.
3. Drain existing manual operations. Enable the durable lock protocol by annotating the existing `default/jaeger-demo-deployment-revisions` ConfigMap with `jaegertracing.io/deployment-lock-version=v1`. The operator must preserve the ConfigMap UID and data. Confirm every manual writer uses the lock-aware workflow revision. Confirm one negative cross-writer test and one successful handoff in rehearsal before production authorization.
4. Deploy the independently owned planner and receiver after review. The planner prepares each day's slot automatically. The receiver invokes `execute` with the reviewed configuration, independently approved receipt and a fixed trusted application function. That function is responsible for authenticated artifact lookup, drift detection under lock, application and durable post-deployment evidence. It returns success only after verification. The module provides the request/lock protocol; production storage, credentials and the complete five-component execution adapter require separate integration.
5. Activate `DEMO_SCHEDULE_ENABLED` after manual verification. The current GitHub entry accepts `scripts-v1` slots. The protocol also distinguishes `helmfile-v1` for the separate Helmfile adoption and verification procedure. Changing the accepted engine requires a reviewed workflow update.

For review, generate the public RBAC/admission manifest using the actual usernames established by authenticated discovery:

```bash
python3 scripts/demo/rehearsals/scheduled-request/access.py \
  --requester "$scheduled_username" --preparer "$preparer_username" \
  --executor "$executor_username" --recovery "$recovery_username" --phase guard > reviewed-request-guard.json
```

After the approved guard is installed, repeat the render with `--phase verified-binding` on the intended operator context. That phase performs a nonpersistent API admission readback before emitting the binding.

Run the following command from the trusted planner after producing a new immutable bundle under the previously approved release policy. The receipt file has exactly `configurationSHA` and `bundleSHA256`. The command reuses the fixed request object for the next UTC date and preserves an unresolved failure. The requester cannot prepare new inputs. Chart and image bytes, keys and private manifests stay out of this record.

```bash
python3 scripts/demo/scheduled_request.py prepare --engine scripts-v1 --receipt "$public_receipt"
```

## Concurrency and recovery

The executor and manual writers acquire the `jaegertracing.io/deployment-lock-holder` annotation on the checkpoint using an atomic test of its UID and resource version. The owner value identifies a GitHub run and attempt or a daily slot and request UID. Successful verification moves a request from `running` to `verified`. The executor releases its lock before announcing `succeeded`. Failures during mutation or verification leave the lock held. A timeout, cancellation or a disabled cron switch cannot release another writer's lock. Future manual jobs honor the installed lock protocol even while cron is paused.

Pause cron and identify the lock owner, request slot, configuration commit and bundle digest. Inspect the executor and GitHub run, verify the owner has stopped, check live Helm status and critical resources, and reconcile any partial deployment. Keep the evidence with the durable operation record. For a `verified` operation, confirm that the receipt records successful deployment and health checks for this exact bundle. For a `failed` operation, record how you restored a safe state. Pause or drain manual operations while inspecting the two records. A signalled request remains eligible during its short claim window and a running executor can finish after cron is paused.

On the recovery operator's own Kubernetes context, inspect the current request and checkpoint. Record their UIDs and resource versions, the expected engine, the public receipt from independent custody and your verified Kubernetes username. After confirming the owning process has stopped and the outcome is reconciled, run:

```bash
python3 scripts/demo/scheduled_request.py recover \
  --operator "$recovery_username" --slot "$slot" --engine "$reviewed_engine" \
  --receipt "$public_receipt" --request-uid "$request_uid" --request-version "$request_rv" \
  --checkpoint-uid "$checkpoint_uid" --checkpoint-version "$checkpoint_rv" \
  --confirmed-stopped-and-reconciled
```

The command checks the authenticated username, both observed identities and versions, the frozen receipt, request state and expected lock owner. It removes only that daily holder. It then records `failed → recovered` or `verified → succeeded` without applying the bundle. The former preserves the original failure for GitHub and allows preparation of a later date. Native admission restricts both mutations to this recovery flow. If the unlock took effect but finalization failed, re-inspect the two records and retry with current versions after confirming the checkpoint is free. A foreign owner must finish before retry. Keep unresolved `running` or manual operations under operator incident handling.

## Local verification

The [API rehearsal](../scripts/demo/rehearsals/scheduled-request/README.md) tests independent principals, native admission, expiry, idempotency and contention with a manual holder. The [monitoring rehearsal](../scripts/demo/rehearsals/monitoring-writer/README.md) tests the pinned chart hooks, chart Secret continuity and rollback on a schedulable node. Production acceptance additionally requires the full five-component adapter, actual identity and permission checks, successful first scheduled run and public functional checks.
