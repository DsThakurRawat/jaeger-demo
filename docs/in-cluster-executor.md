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

## Release and installation

The candidate runtime targets Linux AMD64 on OKE. Pull request CI builds it in a fresh checkout and loads it into a disposable kind node. A project maintainer can publish a reviewed commit after merge through **Publish the demo executor image** on `main`. Supply the full commit as `configuration_sha` and retain the workflow URL and its `ghcr.io/jaegertracing/jaeger-demo-executor@sha256:…` result. The publish job has GitHub Packages write access and no cluster credentials. The release packages Helm 4.3.2, kubectl 1.36.1 and the reviewed Python dependency wheels. The Containerfile checks each downloaded CLI archive or binary against its platform checksum. [Build](../ops/executor/Containerfile) · [Package publication](../.github/workflows/release-executor.yml)

During preparation, the pod reads the exact public `jaeger-demo/main` commit. It treats configuration blobs as data and checks compatibility against the code in the installed image. Chart versions must match the reviewed archive checksums. The Jaeger chart comes from a pinned Git commit with four checked dependency archives. Both snapshot references bind the independent core commit tag, registry index digest and Linux AMD64 manifest digest. A runtime-code change requires a matching image release. A chart bump requires its version and archive checksum in the same reviewed change. [Public input acquisition](../scripts/demo/scheduled_inputs.py) · [Chart archive checksums](../ops/executor/chart-archives.json)

### Installation approval

Render the candidate offline. Keep the image digest, configuration commit, exact object list, permission evidence and private key custody destination with the change record. The renderer emits a suspended CronJob and a Deployment at zero replicas. The bootstrap Job is the sole pod launched by applying this manifest. It has no API token and initializes the two directories on the new claim.

```bash
python3 ops/executor/workloads.py --image "$release_image" --storage-class oci-bv > workloads.json
```

The storage request is one new 50 GiB ReadWriteOnce PVC. OKE can provision a billable block volume when the bootstrap Job first mounts it. The receiver and preparer share the volume on the same worker. Each requests 100m CPU, 256 MiB memory and 256 MiB ephemeral storage. Required pod affinity places the preparer on the receiver’s node. Inspect current node pod slots, CSI attachment capacity, allocatable requests, reclaim policy, encryption and headroom for monitoring hook Jobs and rollout surges before asking to install. The operations pods accept no inbound connections. The rendered NetworkPolicy allows egress needed for DNS, the Kubernetes API, artifacts, registry and health endpoints. It imposes no destination allowlist. Restrict access to creating or modifying pods and Jobs in this namespace, since that access could expose the key mounts. [OCI Block Volume provisioning](https://docs.oracle.com/en-us/iaas/Content/ContEng/Tasks/contengcreatingpersistentvolumeclaim_topic-Provisioning_PVCs_on_BV.htm) · [Kubernetes volume access modes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/#access-modes)

Generate the two key pairs under operator custody only after installation approval. Create `jaeger-demo-preparer-keys` and `jaeger-demo-executor-keys` separately from the files listed in the table above. Supply Secret content from files, keep shell tracing disabled, and record only the public key fingerprints in the installation receipt. Match any private recovery copy to an explicitly authorized shared vault destination. Rehearse public-key readback, encrypted bundle readback and failure recovery with the isolated identities before enabling any scheduled signal. Keep each runnable pod at the tested image digest. [Custody, rotation and retention](daily-bundle-custody.md)

### Activation approval

With the daily GitHub switch off, drain manual writers, check the shared lock and install the reviewed admission rules before the corresponding role bindings. Scale the receiver to one and check readiness. Use a one-off preparer Job copied from the suspended CronJob to validate the six installed releases, the PVC binding, the verified snapshot images and today's signed receipt without sending a deployment request. Match that receipt to the configuration commit that the GitHub requester would observe. Compare the actual effective permissions and negative admission probes with the approved installation. Record any refusal before proceeding. [Actor bindings and recovery](daily-update-request.md#set-up-once)

Ask the maintainer separately before enabling the 12:20 UTC preparer and the 13:00 UTC GitHub signal. Turn on the preparer first, confirm it can publish the intended slot, then set the repository variable `DEMO_SCHEDULE_ENABLED=true` for that slot. To pause, clear that variable and suspend the preparer CronJob. Check both the request state and the lock before scaling down the receiver or making a manual change. Keep incident and prior rollback bundles until the recovery operator closes the case. A clean shutdown preserves the PVC and both private key Secrets. The 32-slot capacity limit requires an operator retention review before the store fills. [Daily release checks](releases.md) · [Custody retention](daily-bundle-custody.md)
