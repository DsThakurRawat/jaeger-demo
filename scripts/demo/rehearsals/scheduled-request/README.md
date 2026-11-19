<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Daily requester API rehearsal

The locally tested Kubernetes 1.36 API enforced the independently authenticated requester, preparer and receiver roles. The test used a fourth identity for a concurrent manual operator. The requester signalled the planned UTC slot, the receiver acquired the same lock used by manual operations, and duplicate calls observed one verified result. Kubernetes denied changes to the prepared digest and configuration and denied direct access to Helm Secrets, workloads, webhooks, RBAC and policy. A failed verification retained the lock and prevented the preparer from replacing an unresolved slot. The tests ran against a loopback API server with a fresh etcd database and synthetic receipts. A successful callback simulated full application verification. [28 API checks](api-receipt.json) · [Runtime hook and rollback verification](../monitoring-writer/README.md)

The guard and role binding are separate installation phases. Admission configuration can take time to reach an API server. The bootstrap check sends a server-side dry run that must be denied by the installed policy before the guarded role is granted. It leaves the control record untouched. [Reviewed access generator](access.py)

## Repeat on a disposable cluster

Install kind, kubectl, Python 3 with PyYAML, and a working container runtime. From the repository root:

```bash
set -eu
work=$(mktemp -d)
kind create cluster --name jaeger-request-rehearsal --image kindest/node:v1.36.1 \
  --kubeconfig "$work/kubeconfig" --wait 5m
python3 scripts/demo/rehearsals/scheduled-request/probe_api.py \
  --kubeconfig "$work/kubeconfig" --receipt "$work/request-api.json"
kind delete cluster --name jaeger-request-rehearsal --kubeconfig "$work/kubeconfig"
```

The driver requires one loopback cluster with the expected local name, context, certificate authentication and node inventory. It rejects pre-existing control state. Keep the receipt and remove the temporary directory when finished. The [operator handoff](../../../../docs/daily-update-request.md) covers setup, runtime custody and recovery.
