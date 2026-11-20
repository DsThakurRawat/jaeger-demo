<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# OpenSearch chart upgrade and rollback fixture

The optional storage phase of the loopback rehearsal installs the real OpenSearch and Dashboards 3.7.0 charts with the repo's values. An administrator seeds two releases, a Pod, a 50 GiB bound claim and a prebound PV. The API-only fixture supplies the binding status directly. Its StatefulSet retains the 10 GiB claim template from the public chart values.

The Dashboards chart has a binding to an external Role. The fixture creates an empty Role with that name so its admission test can exercise an attempted subject change. The restricted writer upgrades each release to a candidate with a marker annotation on its Service and pod template, then rolls it back to its original chart and values. The OpenSearch engine version stays at 3.7.0 in both revisions. At every step the scheduled reconciler's binding check reads the same Pod mount, PVC UID and bound PV. All ten chart objects retain their UIDs. The fixture separately checks both Service specs, the workload and configuration bodies, and the RoleBinding. [Run receipt](storage-chart-receipt.json)

## Writer access exercised

The fixture removes the six-release history test's bindings before granting the storage-only Role. The writer has namespace-scoped Secret GET/LIST/CREATE/UPDATE for Helm history, named GET/PATCH on the ten existing chart objects, and GET on the one expected Pod and PVC. Helm's history envelope protects Secret modifications. Helm requires Secret LIST even for a named release. The fixture can therefore read other Secrets in the storage namespace.

Helm 4.3.2 uses server-side apply. Its successful API calls included PATCH on ConfigMaps, Services, a ServiceAccount, a PodDisruptionBudget, a StatefulSet, a Deployment and a RoleBinding. Several of those objects were identical across revisions. Granting PATCH only on the resources whose rendered content changes fails the transaction. The recorded API operations include the verifier's named GETs and the explicit denial tests.

A fixture-admin-owned, loopback-only admission webhook checks the API's old and proposed objects against the reviewed baseline and candidate. It compares labels, annotations, subjects, image references, pod templates, selectors, configuration and other content. It excludes server-managed resource version, generation, creation timestamp, managed fields and status. Kubernetes authorizes the status subresource separately. The fixture writer cannot register, replace or bypass the webhook using its Role. The writer cannot mutate PVCs, create Pods, read Secrets from another namespace, reroute a Service, change a binding subject or apply an unreviewed workload.

## Reproduce

Use the same checked [envtest and Helm prerequisites](README.md#reproduce). Download the public, pinned chart archives into a private directory. The fixture checks their SHA-256 digests against the checked-in [chart inventory](../../../../docs/all-five-chart-access.json).

```bash
mkdir -p .tools/storage-charts
helm pull --repo https://opensearch-project.github.io/helm-charts \
  opensearch --version 3.7.0 --destination .tools/storage-charts
helm pull --repo https://opensearch-project.github.io/helm-charts \
  opensearch-dashboards --version 3.7.0 --destination .tools/storage-charts
python3 scripts/demo/rehearsals/helm-history/rehearse-six-release-history.py \
  --assets /path/to/envtest/bin --storage-charts .tools/storage-charts \
  --receipt .tools/storage-charts/receipt.json
```

The command starts the private API server and etcd, exercises the original six-release history cases, then runs the two actual storage charts. It records the storage result under `storageChartRehearsal`. All Pods are API records in this fixture. No Pod is scheduled and no disk is mounted. The fixture uses `--wait=hookOnly` for that reason. Production rollout readiness, OCI CSI persistence, both running versions, trace write/read and the OKE principal still require verification. An engine-version change also needs a review of compatibility and storage effects. A same-version fixture rollback supplies no evidence for a production engine downgrade.

## Mounted-file continuity on kind

A separate, schedulable kind test uses the local-path provisioner and a small BusyBox StatefulSet. It writes a random marker under the mounted OpenSearch data path, replaces the Pod and reads the marker back. The replacement has a different Pod UID. The same production binding reader reports the original PVC UID and PV, and the PV's claim reference matches that PVC. The test removes its temporary namespace and waits for the fixture PV to be deleted. [Kind receipt](kind-storage-receipt.json)

To reproduce, use an existing local kind cluster with a `kind-jaeger-*` context, its explicit fixture kubeconfig, a schedulable node and the standard kind local-path provisioner.

```bash
python3 scripts/demo/rehearsals/helm-history/kind-storage-continuity.py \
  --kubeconfig /path/to/fixture.kubeconfig --context kind-jaeger-hook-rehearsal \
  --receipt .tools/storage-charts/kind-storage.json
```

The kind test uses a 64 MiB disposable claim in its own namespace. It exercises real Kubernetes binding, mounting and persistence; it uses fixture administrator access for setup and Pod restart. The separate API-only chart test verifies the restricted writer and exact chart operations. OCI CSI, OpenSearch or Dashboards startup and fresh trace verification remain runtime checks.

## Remaining workload checks for the daily job

- **OpenSearch and Dashboards.** The production scripts also wait for workload rollouts, check the running engine version in `opensearch-cluster-single-0`, read the public Dashboards version and write/query one trace through the Jaeger Services. Pod exec reaches more than the one version request, and dynamically chosen port-forward targets can reach more than the two required ports. Rehearse either the actual restricted subresource checks or a protected in-cluster verifier with fixed requests before a production grant. The local fixture grants neither capability.
- **OCI Jaeger and OTel Jaeger.** The extracted scripts need a reviewed update and possible atomic rollback of the existing Jaeger release in each stack, plus named generator and ingress reconciliation. Rehearse the exact rendered images, configs, pod templates, Service targets, ingress routes, permitted generator restart and rollback. OTel also needs its named query Service. Verify the chart-owned HotROD identity and the scheduled in-place update.
- **OTel sample application.** The chart includes application Deployments, an agent DaemonSet, Services, ConfigMaps, ServiceAccounts and the collector's ClusterRole and ClusterRoleBinding. Named PATCH on these objects still needs independent content checks for the pod specs, rules and binding subject. Check workload readiness and checkout trace/SPM verification through the same restricted path.
- **Deployment checkpoint.** A successful component update merge-patches its entry in the named revisions ConfigMap. Rehearse an allowed single-key write and denied unrelated changes. A repository switch and successful real scheduled job are separate production acceptance steps. [Scheduled access plan](../../../../docs/all-five-schedule-access.md)
