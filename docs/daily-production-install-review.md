<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Review the daily executor installation

Run the packet generator from the reviewed `jaeger-demo` checkout. It renders
the request controls, native workload and history guards, typed admission
parameters, access grants, network rules, storage initializer and inactive
runtime into separate, ordered Kubernetes Lists. Each artifact has a SHA-256
in `review.json`. The generator runs offline. Use a private directory outside
Git for the inputs, hook output, review packet and operator records.

Capture the exact Kubernetes usernames from authenticated `kubectl auth whoami
-o json` in each intended context. Put the `status.userInfo.username` values
in a private JSON file with keys `requester`, `preparer`, `executor`,
`recovery` and `bootstrap`. The preparer and executor must match the service
accounts in the workload manifest. The other three identities must be distinct
from these accounts and from each other. Separately verify the selected
cluster's `kube-system` namespace UID, the published executor image digest,
and the intended RWO storage class. The packet records these private inputs so
another maintainer can compare them with discovery. Keep OCI identifiers,
credential files and rendered production packets outside the repository.

To include the monitoring hook package, first render it on the verified target
operator context with the existing checked chart and server dry-run procedure.
The renderer verifies the requested cluster UID and the archive checksum,
then returns `guards.json` and `grants.json`. Have the reviewer record the
actual target UID and the SHA-256 of each reviewed file in a separate private
JSON document with keys `clusterUID`, `guardsSHA256` and `grantsSHA256`.
The packet checks this attestation against the supplied bytes. The underlying
hook files do not independently encode the target UID. [Monitoring hook
render procedure](daily-executor-permissions.md#monitoring-hook-identities)

```bash
python3 ops/executor/review_packet.py \
  --actors-file "$actors_file" --cluster-uid "$cluster_uid" \
  --image "$image_by_digest" --storage-class "$storage_class" \
  --hook-directory "$reviewed_hook_directory" \
  --hook-attestation "$hook_attestation_file" --output "$new_packet_directory"
```

For an early review, omit both hook arguments. The index lists the two missing
phases and sets `allManifestPhasesIncluded` to false. A full packet includes the
reviewed hooks, keeps the receiver at zero replicas and suspends the prep
CronJob. Creating the separately staged PVC can provision billable storage.
Creating the volume initializer Job starts that Job.

Compare the printed review digest with the independently received packet and
verify every phase before using it. This read-only check runs from the generated
directory.

```bash
shasum -a 256 review.json
jq -r '.stages[] | select(.file) | "\(.sha256)  \(.file)"' review.json | shasum -a 256 -c
jq -r '.stages[] | [.phase, .objects, (.gate // .pending)] | @tsv' review.json
```

Use the stage numbers for **review and dependency order**. Confirm the live prerequisites in the index.
The target cluster, bootstrap resource UIDs, existing protocol and empty lock,
effective permissions, release baselines, key projections and budgeted capacity
all require verification. Seed only absent objects. Preserve an existing
control or dated parameter record. Create or server-side apply the immutable
monitoring hook archive to avoid the client-side annotation size limit. Wait
for the CRD and all admission policies and bindings to be effective before any
write grants. Verify actual dry-run denial under each relevant identity and on
every serving API endpoint.

The requester RoleBinding in phase 21 is a **proposal**. After approved guard
installation, run the existing `access.py --phase verified-binding` from the
verified bootstrap context. Confirm its enforcement probe returns the named
request-policy denial, then compare the emitted binding with the RoleBinding
in phase 21. Confirm the recovery principal's pre-existing, separately reviewed
access to the named checkpoint through its recovery guards. Review the
preparer and executor's full Secret get/list scope in four namespaces, the
history writer and privileged monitoring hook permissions. Provision custody
keys through their separate private procedure. Activation, the first dated run
and Slack verification are distinct operator decisions. [Request and recovery
procedure](daily-update-request.md) · [Admission and grants](daily-executor-permissions.md)
· [Private custody](daily-bundle-custody.md)
