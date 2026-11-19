<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Executor workload rehearsal

Run the unpublished candidate image by its OCI manifest digest on a fresh, single-node `kind-jaeger-executor-ci` cluster with Kubernetes v1.36.1. The node must already contain the exact `ghcr.io/jaegertracing/jaeger-demo-executor@sha256:…` reference. The harness selects the kind kubeconfig, verifies the loopback server and node, then reads the cluster UID used by the workload generator. It requires Python 3, kubectl, kind and OpenSSL 3. [CI build and image import](../../../../.github/workflows/executor-ci.yml)

```bash
EXERCISE_IMAGE="$candidate_by_digest" bash scripts/demo/rehearsals/executor-runtime/run.sh \
  --receipt "$new_receipt_path"
```

The bootstrap Job imports the candidate's original receiver, custody, approval, inputs and application modules. The preparer and executor run the original image entry point under their actual service accounts. The fixture then substitutes read-only synthetic inputs, two fixture approval partitions, application checks and clock via ConfigMap mounts in these two Pods. The helper observes both workload states before activating the executor and creating a one-time preparer Job. The CronJob stays suspended.

Linux AMD64 CI retains the production node selector. A local ARM64 rehearsal selects the verified ARM64 kind node and uses an ARM64 candidate image. The synthetic adapter checks the distinct key projections, the read-only executor bundle mount, the preparer's lack of an outcome mount and the executor's writable outcome mount. It verifies all frozen synthetic bytes after decryption. The synthetic admission checks require both partition records to match the signalled slot and receipt. The preparer compares their combined digest against the signed index. The two daily transactions exercise success, executor replacement, failed verification, retained lock and the same controller-bound PVC. The application adapter's real chart mutations and public health checks require their separate verification. The fixture uses node-local storage and temporary keys. OCI volume topology, backup and real identity grants require production acceptance. [Receiver and recovery](../../../../docs/in-cluster-executor.md)

The Linux arm64 candidate built from `f5a623cc86c078eae33e862f3ca7acecdea8133b` passed all ten Pod and PVC checks on Kubernetes 1.36.1 before typed admission publication was added. The separate [API receipt](../scheduled-request/receiver-receipt.json) also covers a stale partition and interrupted publication. The receipt identifies the unpublished OCI digest loaded into the node. [Recorded workload checks](receipt.json)

The harness leaves the executor scaled to zero after success. Inspect any failed fixture Job in the dedicated namespace. Delete the disposable kind cluster when all rehearsals using it are complete.
