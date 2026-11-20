<!--
Copyright (c) 2026 The Jaeger Authors.
SPDX-License-Identifier: Apache-2.0
-->

# Manual OpenSearch recovery procedure

Use this procedure when a maintainer elects to preserve historical trace data
and has the required storage permissions and restore capacity. The daily hosted
job upgrades in place on the live PVC and verifies new traces. It creates no
snapshot or restore workload. Manual recovery includes two independent repairs:
[running API requests in the selected pod](https://github.com/jaegertracing/jaeger-demo/pull/13)
and [starting the isolated database with one security setting](https://github.com/jaegertracing/jaeger-demo/pull/14).
Confirm the chosen revision includes both fixes before relying on a manual test.

The recovery workflow creates a full OCI Block Volume backup, restores it to a
new PVC, and tests it with the exact OpenSearch and Jaeger image digests running
in production. It does not modify the live PVC or expose the restored database
through a Service or ingress, and both query listeners bind to loopback. It
removes only the exact run-specific disposable restore pod after success or
failure and retains the snapshot, restored PVC, and configuration for recovery
evidence.

The workflow runs `curl` through `kubectl exec` in the `opensearch` container.
The source flush uses the live OpenSearch pod. The restored index, service and
trace queries use the isolated restore pod, where the OpenSearch and Jaeger
containers share a loopback interface.

## Preserve the recovery record

After a successful workflow run, record the following in a private operational
record:

- the `VolumeSnapshot` and bound `VolumeSnapshotContent` names;
- the source OpenSearch version and image digest annotations;
- the restore-tested timestamp and tested Jaeger image digest;
- the source storage class and restored PVC capacity; and
- the CSI snapshot handle from the retained `VolumeSnapshotContent`.

The CSI snapshot handle identifies private infrastructure. Do not paste it into
an issue, pull request, or public Actions log. Keep the `VolumeSnapshotContent`
and OCI backup for the retention period agreed for that manual operation. Obtain separate approval before deleting retained storage.

## Restore while the namespace exists

Create a new PVC in the `opensearch` namespace whose `dataSource` is the tested
`VolumeSnapshot`. Use the live PVC capacity, not the chart's smaller requested
size. Boot the recorded OpenSearch image digest against that new claim with
security and discovery settings matching the source. Keep it isolated from all
Services and clients until its primary shards recover and the same inventory,
document-floor, service-query, and trace-query checks pass.

Never point the tested snapshot at the live PVC and never use `helm rollback`
to downgrade OpenSearch.

## Restore after namespace or workload replacement

The namespaced `VolumeSnapshot` may be gone, but its
`VolumeSnapshotContent` and OCI backup remain because their deletion policy is
`Retain`. In a private administrative session:

1. Read the retained content's CSI snapshot handle and recorded source image
   digest. Stop if either is missing.
2. Create a new static `VolumeSnapshotContent` with a new name, the same
   `blockvolume.csi.oraclecloud.com` driver and snapshot handle, and
   `deletionPolicy: Retain`. Bind it to a new `VolumeSnapshot` in the recovery
   namespace through `spec.volumeSnapshotRef`.
3. Create the namespaced `VolumeSnapshot` using
   `spec.source.volumeSnapshotContentName`, wait for `readyToUse=true`, and
   provision a new PVC from it at the recorded capacity.
4. Boot an isolated single-node OpenSearch using the exact recorded source
   image digest. Confirm non-red health, exact Jaeger index/template/alias
   inventory, document floors, and representative Jaeger service and trace
   queries.
5. Recover service through a fresh installation of that prior OpenSearch
   version and the new restored volume. Reconnect Jaeger only after explicit
   approval and a final query check.

Do not delete the retained content or OCI backup during recovery. If the CSI
snapshot APIs, retained content, backup handle, exact image digest, or capacity
are unavailable, stop and use an OCI Block Volume restore procedure rather than
improvising against the live volume.
