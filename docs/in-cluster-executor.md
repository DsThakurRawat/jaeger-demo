<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# In-cluster scheduled executor

The trusted preparer publishes the day's private, signed bundle. The executor independently authenticates that bundle and claims the same durable writer lock used by manual operations. The GitHub job supplies one signal and observes the result. The manifests, service accounts, keys and cluster permissions require operator installation before activation. [Daily request protocol](daily-update-request.md) · [Receiver entry point](../scripts/demo/scheduled_receiver.py)

## Processes and credentials

Run `python3 /opt/jaeger-demo/scripts/demo/scheduled_receiver.py prepare` as the `jaeger-demo-preparer` service account and `serve` as `jaeger-demo-executor`, both in `jaeger-demo-operations`. Each process queries the Kubernetes API for its authenticated username before acting. It writes a temporary kubeconfig pointing at the in-cluster TLS endpoint, projected CA and rotating token file. This kubeconfig also supplies the identity for the fixed application commands.

| Mount | Preparer | Executor |
| --- | --- | --- |
| `/var/run/secrets/kubernetes.io/serviceaccount` | Projected preparer token, CA and namespace | Projected executor token, CA and namespace |
| `/var/lib/jaeger-bundles` | Publish signed index and encrypted bytes | Read only |
| `/var/run/jaeger-bundle-keys` | `signing-key.pem`, `recipient-public.pem`, `verification-key.pem` | `verification-key.pem`, `decryption-key.pem` |
| `/var/lib/jaeger-outcomes` | No mount | Append durable public outcome records |
| `/tmp` | Private scratch directory | Private scratch directory and readiness file |

Run the preparer between 11:00 and 15:00 UTC for the 13:00 UTC slot. If an authenticated slot already exists it reuses the frozen receipt. Otherwise it obtains the reviewed main SHA from the fixed resolver, prepares the bundle and publishes it. An invalid existing index stops preparation. The GitHub signal and executor claim retain their separate expiry and five-minute acknowledgement deadline.

The receiver polls the fixed control record every ten seconds. A valid signal must match the reviewed engine, the eligible UTC slot and the signed receipt found independently in custody. The code passes only authenticated, decrypted member bytes to the fixed application adapter. Each mutation must check the original request UID, receipt, state and lock holder. After all five checks succeed, the receiver writes its durable verification record, updates the request, releases the lock and reports success. A failed check leaves a failure record and the lock.

## Operations and recovery

Use `python3 /opt/jaeger-demo/scripts/demo/scheduled_receiver.py ready` for the readiness probe. It checks the last successful control observation, or the elapsed time of an active operation. A ready receiver can be observing a blocked claim. Its state-change log identifies `inspect-running`, `inspect-verified` or `inspect-failed`. An unreadable control record removes readiness. Receiver-generated logs and outcome files contain public operation metadata only.

Inspect the request, shared checkpoint and `<UTC-date>.<bundle-sha256>.<phase>.json` in the outcomes mount. The `started` file is exclusive. A replacement process observes a prior claim without executing it again. A `started` record without a terminal outcome requires inspection. The application's hard execution budget is 275 minutes. On expiry the Python container entrypoint exits, so Kubernetes terminates its children and the unconfirmed operation retains its lock. Confirm the owning Pod and its subprocesses have stopped, inspect the live releases and receipt, then follow the operator procedure. A `running` claim requires incident handling. A clock or readiness timeout cannot establish that the owner has stopped. [Recovery procedure](daily-update-request.md#concurrency-and-recovery)

## Rehearsal

The local Kubernetes 1.36.0 rehearsal used two independent processes, real API-issued service account tokens, separate projected key directories, a signed index and encrypted synthetic bundle bytes. It exercised preparation, success, failure, abrupt process exit and restart. The fixed application callback verified fixture files under lock. Both processes ran under the same local OS user, so this test covers Kubernetes authentication and the protocol. Pod security contexts and volume isolation require the separate workload rehearsal. [Recorded checks](../scripts/demo/rehearsals/scheduled-request/receiver-receipt.json)

To repeat on a fresh disposable `kind-jaeger-request-rehearsal` cluster with the repository's Python dependencies installed, run:

```bash
python3 scripts/demo/rehearsals/scheduled-request/probe_receiver.py \
  --kubeconfig "$kind_kubeconfig" --receipt "$new_receipt_path"
```
