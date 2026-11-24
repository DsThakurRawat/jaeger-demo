<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Demo node pool

The demos run on the existing OKE cluster, which is outside this repository's
deployment scope ([migration plan](MIGRATION.md#ownership-and-fixed-constraints)).
This page is for the operator who changes its node pool or replaces its node: a
node pool update, a Kubernetes or image upgrade, node cycling, or a failed node
that OKE replaces. No workflow in this repository runs these commands, and
nothing here applies [ops/oke/node-pool-init.sh](../ops/oke/node-pool-init.sh).

## Why the root filesystem needs growing

A node's boot volume can be larger than its image's default, but the root
partition keeps the image's size until it is extended. On November 23, 2026 the
demo's only node used 29.4 GB of its 100 GB boot volume for the root filesystem,
reached disk pressure, and kubelet evicted Pods. An operator grew it online with
`oci-growfs` ([evidence](MIGRATION.md#current-evidence-and-unresolved-gates)).
That fix lives on that instance only. A node that OKE creates later boots with the
default-sized root partition again unless the node pool's initialization script
grows it ([#120](https://github.com/jaegertracing/jaeger-demo/issues/120)).

## The initialization script

[ops/oke/node-pool-init.sh](../ops/oke/node-pool-init.sh) is Oracle's documented
script for this case, example 5 of
[Using custom cloud-init scripts](https://docs.oracle.com/en-us/iaas/Content/ContEng/Tasks/contengusingcustomcloudinitscripts.htm).
cloud-init runs it once, when an instance in the pool first boots. It:

1. fetches the default OKE init script from the instance metadata service, as the
   default OKE logic does;
2. runs `/usr/libexec/oci-growfs -y`, which extends the root partition and grows
   the filesystem to the boot volume size;
3. runs the OKE init script, unchanged, which registers the node.

**Failure handling.** The script has no `set -e`, as in Oracle's example. When
`oci-growfs` fails, or is missing because the image is not an Oracle Linux
platform image with `oci-utils`, cloud-init logs the error to the node's
cloud-init output and step 3 still runs. The node then registers with the
default-sized root filesystem, which is the state before this change and is
caught by the [verification](#3-verify-on-the-next-new-node) below. Stopping
instead would leave a node that never registers, and with one node in the pool
that takes both demos down. The script's exit status is the OKE init script's, so
a failed registration still shows as a failed cloud-init run. The two OKE lines are
copied byte for byte; `node_pool_init_test.py` fails if they or the order change.

## 1. Inspect the node pool (read only)

Use an administrator's OCI CLI profile that can read and update the node pool,
`jq`, and a checkout of a reviewed commit on `main`. Run the commands from the
repository root. The node pool and cluster OCIDs, the node names (on OKE they are
the nodes' private IP addresses) and any metadata values are private
infrastructure data: keep them out of commits, issues, pull requests and public
logs, as the placeholders below do.

Find the node pool's OCID with
`oci ce node-pool list --compartment-id <compartment-ocid> --cluster-id <cluster-ocid> --query 'data[].{name:name,id:id}'`,
then read it. These commands change nothing and print no OCID or metadata value:

```bash
node_pool_id='<node-pool-ocid>'
oci ce node-pool get --node-pool-id "$node_pool_id" | jq '{
  name: .data.name,
  kubernetes_version: .data["kubernetes-version"],
  node_shape: .data["node-shape"],
  size: .data["node-config-details"].size,
  boot_volume_size_in_gbs: .data["node-source-details"]["boot-volume-size-in-gbs"],
  node_metadata_keys: ((.data["node-metadata"] // {}) | keys)
}'
if oci ce node-pool get --node-pool-id "$node_pool_id" \
    | jq -r '.data["node-metadata"].user_data // empty' \
    | base64 --decode | cmp -s - ops/oke/node-pool-init.sh; then
  echo 'user_data is ops/oke/node-pool-init.sh'
else
  echo 'user_data is not set or differs from ops/oke/node-pool-init.sh'
fi
```

- `node_metadata_keys` without `user_data`: the pool has no initialization script.
  Read on November 23, 2026, the demo's pool was in this state
  ([#120](https://github.com/jaegertracing/jaeger-demo/issues/120#issuecomment-7101900372)).
- `user_data is ops/oke/node-pool-init.sh`: the script is set. Skip to
  [step 3](#3-verify-on-the-next-new-node).
- `user_data` listed but different: **stop.** The pool runs another
  initialization script, and step 2 would replace it. Read it privately and bring
  it to the owner; combining two scripts is a new change to review here first.

## 2. Set the initialization script (approved operator action)

Setting the script changes the externally owned cluster. Do it only after the
owner approves this change, and record the approval, the time and the repository
commit in the release record.

`oci ce node-pool update --node-metadata` **replaces the whole metadata map**: the
OCI CLI warns that updates to `node-metadata` "will replace any existing values"
and asks for confirmation unless `--force` is given
([source of `oci ce node-pool update`](https://github.com/oracle/oci-cli/blob/v3.96.3/services/container_engine/src/oci_cli_container_engine/generated/containerengine_cli.py),
[command reference](https://docs.oracle.com/en-us/iaas/tools/oci-cli/latest/oci_cli_docs/cmdref/ce/node-pool/update.html)).
So the commands read the current metadata and add `user_data` to it, rather than
passing `{"user_data": ...}` alone. They build the value from the committed file,
so no copy of the script can drift. Oracle's inline
`'{"user_data": "'$(cat <file> | base64)'"}'` form is not used: GNU `base64` wraps
its output, and the unquoted substitution then splits into several arguments.

First prepare the new metadata. This changes nothing:

```bash
node_pool_id='<node-pool-ocid>'
work_dir="$(mktemp -d)"
oci ce node-pool get --node-pool-id "$node_pool_id" >"$work_dir/node-pool.json"
user_data="$(base64 <ops/oke/node-pool-init.sh | tr -d '\n')"
jq --arg user_data "$user_data" '(.data["node-metadata"] // {}) + {user_data: $user_data}' \
  "$work_dir/node-pool.json" >"$work_dir/node-metadata.json"
jq 'keys' "$work_dir/node-metadata.json"
```

The keys must be the `node_metadata_keys` from step 1 plus `user_data`. Stop if
any command failed or the keys differ. Then apply it:

```bash
oci ce node-pool update --node-pool-id "$node_pool_id" \
  --node-metadata "file://$work_dir/node-metadata.json" \
  --if-match "$(jq -r '.etag' "$work_dir/node-pool.json")" \
  --force --wait-for-state SUCCEEDED --wait-for-state FAILED
rm -rf "$work_dir"
```

- `--if-match` makes the update fail if the node pool changed since it was read.
  Read and prepare again in that case.
- Pass no other property. In particular, a change to the placement configuration
  terminates the existing nodes and creates new ones
  ([modifying node pool properties](https://docs.oracle.com/en-us/iaas/Content/ContEng/Tasks/contengmodifyingnodepool.htm)).
- The update does not touch the running node. Node pool property changes apply
  only to nodes created afterwards (same page). The current node keeps its grown
  filesystem and does not run the script.
- Run step 1 again. It must print `user_data is ops/oke/node-pool-init.sh` and
  list the same other keys as before. That read-back, not the work request state,
  is the evidence for the record.

## 3. Verify on the next new node

The script runs only on a node the pool creates after step 2. Whether to replace
the node deliberately to prove it, or to wait for the next planned upgrade or
replacement, is the owner's decision: with one node in the pool a replacement
interrupts both demos unless the pool is first scaled up.

On the first new node, with the administrator kubeconfig, read the node
filesystem from the kubelet summary API. This is the `df -h /` figure without
starting anything on the node. `node.fs` is the filesystem that holds the
kubelet's directory, `/var/lib/kubelet`, and the one whose pressure evicted Pods
on November 23. On the OKE Oracle Linux images that directory is expected to be on
the root filesystem; this page has not yet been checked against `df -h /` on a
node:

```bash
kubectl get nodes -o custom-columns='NAME:.metadata.name,CREATED:.metadata.creationTimestamp,VERSION:.status.nodeInfo.kubeletVersion'
node='<node-name>'
kubectl get --raw "/api/v1/nodes/$node/proxy/stats/summary" | jq '.node.fs | {
  capacity_gb: (.capacityBytes / 1e9),
  available_gb: (.availableBytes / 1e9),
  used_gb: (.usedBytes / 1e9)
}'
kubectl get node "$node" -o jsonpath='{.status.conditions[?(@.type=="DiskPressure")].status}{"\n"}'
```

**Acceptance** ([#120](https://github.com/jaegertracing/jaeger-demo/issues/120)):
the node pool's initialization script runs `oci-growfs` before the OKE init script
(step 2's read-back), and a node created from the pool reports a root filesystem
close to the boot volume size. With the 100 GB boot volume, require
`capacity_gb` of at least 70 and `DiskPressure` `False`. The grown November 23
node reported 82.8 GB; a node without the script reports about 30 GB. Record the
date, the figures and the node's creation time, not its name, in the release
record and the migration plan.

If the new node reports about 30 GB, the script did not grow the filesystem.
Reading the node's cloud-init output needs a privileged Pod on the node, so it
falls under the next section.

## Growing the filesystem on a running node

A node created without the script, or one where `oci-growfs` failed, can be grown
online as on November 23. That needs a privileged node debug Pod, which runs as
root in the host's namespaces with the host filesystem mounted, for example
`kubectl debug node/<node-name> -it --profile=sysadmin --image=<reviewed-image> -- chroot /host /usr/libexec/oci-growfs -y`.
Run it only with the owner's explicit approval for that node and that time,
delete the debug Pod afterwards, and record both in the release record. It fixes
that instance only; step 2 is what covers the next node.
