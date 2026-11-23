<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Private Helmfile deployment bundles

> **Under revision.** [#95](https://github.com/jaegertracing/jaeger-demo/issues/95) replaces the plan-and-apply contract this document implements, and bundle custody is retired with the in-cluster executor. The Helmfile candidate configuration and its validation remain. See the [migration plan](MIGRATION.md) for the current contract.

The planner freezes one reviewed configuration commit, one component selection
and its prerequisites. The executor consumes those bytes under the deployment
lock. An adoption bundle preserves existing image references. A promotion bundle
requires Jaeger and HotROD references with their resolved digests.

The library seals all members in memory with the standard authenticated Fernet
format from Python cryptography. The portable file is named
`<ciphertext-sha256>.fernet`. The public receipt has exactly two fields,
`configurationSHA` and `bundleSHA256`. A new build uses fresh randomness and needs
its own approval. The archive can contain rendered Kubernetes Secrets and private
Helm values. Keep the encrypted file in private custody and the key separately.
Keep plaintext working directories owner-only. Key generation, retention,
recovery and transport for production need an operator decision.

## Planner inputs

Build in a directory owned by the current user with mode 0700. The program derives
the entire member allowlist from the approved selection. Supply these files:

| Member | Contents |
| --- | --- |
| `plan.json` | Format `jaeger-demo-helmfile-plan-v1`, full configuration commit, adoption or promotion mode, stack and component selector, requested release IDs, ordered releases, rendering capabilities and image evidence. |
| `tools.lock.json` | Exact bytes of `scripts/demo/helmfile-tools.lock.json` in the reviewed tree. |
| `image-selection.json`, `image-compatibility.json` | For image promotions that include Jaeger, the exact resolver output and successful compatibility receipt. The receipt binds that file, the selected renders, configuration commit, independent image records and tested Linux manifest. |
| `live.json` | Sanitized baseline projection from the read-only observer. |
| `releases/<namespace>/<name>/chart.tgz` | Complete chart archive including its packaged dependencies. The plan records the chart source, name, version and SHA-256. For a repository chart, supply the unmodified `helm pull` archive. |
| `releases/<namespace>/<name>/values.yaml` | Frozen merged user values, including previously supplied file contents, image inputs and chart flags. |
| `releases/<namespace>/<name>/rendered.yaml` | Exact output from the pinned renderer using these inputs and the recorded capabilities. Preserve byte order and encoding. |

The plan's top-level fields are `format`, `configurationSHA`, `mode`, `selector`,
`requested`, `releases`, `renderContext` and `images`. The selector uses `stack`
and `component`. Each release entry has an `id` of `namespace/name`, ordered
`needs`, `chart`, explicit `helm` parameters, `observe`, `checks` and `rollback`.
The application dependency graph and chart pins come from the reviewed Git tree.
For each of the four repository charts, the checker requires the chart name from
`helmfile.yaml` and the archive SHA-256 that the same commit records for that name
and version in [`scripts/demo/chart-archives.lock.json`](../scripts/demo/chart-archives.lock.json).
A Helmfile chart version without a reviewed checksum in that file cannot be
bundled. Update both in the same reviewed change.
For a locally packaged Jaeger chart, use its upstream repository URL followed by
`@` and the exact pinned commit. The checker requires that source and the chart
name `jaeger`. No reviewed checksum exists for this locally built package, so the
approved bundle digest is the only binding of its bytes. Review that render
against the pinned commit before approval. A companion adoption uses
`component=demo-resources` and the source `charts/demo-resources`. The checker
compares its rendered resource identities and keep annotations against the chart's
committed allowlist and rejects any `helm.sh/hook` annotation. Helm does not own
a hook, ignores its keep policy and by default deletes and recreates it before
each run.

Freeze the Helm operation (`install` or `upgrade`), `atomic`, `wait`,
`timeoutSeconds`, `historyMax`, `skipCRDs` and `createNamespace`. Use the reviewed
existing namespace. Record `renderContext.kubernetesVersion` and sorted unique
`apiVersions`. Each selected Jaeger stack needs separate `images.<stack>.jaeger`
and `hotrod` records with the exact `reference`, a full upstream `sourceCommit`
and a `sha256:` digest. Both references must match the actual rendered Deployment
containers. See the [working fixture](../scripts/demo/adoption_bundle_test.py).

The observer records `cluster`, `healthy`, the SHA-256 of canonical `renderContext`
under `capabilitiesSHA256`, the selected `releases`, `objects` and `rollbackTargets`.
For image-preserving adoption, include `images` from live workloads as well.
Record release status, revision and the decoded release record's `recordSHA256`.
An absent first-install companion uses revision zero and a null digest. For each
`apiVersion:Kind:namespace:name` object, record `uid` and `stateSHA256`, or null
for observed absence. Use `-` for cluster-scoped objects. Include every rendered
identity and relevant additional resources, including PVC bindings and operator
workloads. Select meaningful state such as Service bindings, selectors, desired
pod templates, active images, storage and webhook trust. Exclude timestamps,
resource versions and status noise. Permission failures abort collection. Keep
raw live Secret payloads in private memory. The fingerprint checks use canonical
JSON with sorted keys and compact separators.

A rollback entry records its strategy, revision and digest. The observer must
independently re-read that target, including status. Recovery of OpenSearch and
Dashboards is operator-led. Use de-adoption for the companion release. Application
Helm rollback needs a verified successful stored revision. Verify the named health
checks in the trusted runner and record their outcomes.

## Verify and consume

The CLI accepts an existing owner-only Fernet key file. Install the same
cryptography and PyYAML versions used by demo CI via the existing OCI constraints.
The `build` and `verify` commands operate on a clean checkout of the supplied
commit. The operator or main-only runner must establish that this is the reviewed
commit merged to `main` and obtain the approved receipt independently of the
downloaded bundle.

```sh
python3 scripts/demo/adoption_bundle.py --checkout . \
  --configuration-sha "$CONFIGURATION_SHA" --key-file "$PRIVATE_KEY_FILE" \
  build "$PRIVATE_PLAN_INPUTS" "$PRIVATE_BUNDLE_STORE"
python3 scripts/demo/adoption_bundle.py --checkout . \
  --configuration-sha "$CONFIGURATION_SHA" --key-file "$PRIVATE_KEY_FILE" \
  verify "$PRIVATE_BUNDLE_STORE/$APPROVED_SHA.fernet" --approved-sha "$APPROVED_SHA"
```

Verification emits the receipt. It checks the authenticated payload, every member
digest, safe archive structure, source tree, selection, tool pins, chart names,
versions and reviewed repository chart checksums, and recorded recovery targets. Neither CLI command deploys or exports decrypted files.

The live runner entry point is `consume(path, cipher, approved, checkout, expected,
render_frozen, read_live, reconcile, verify_stack)`. Acquire the deployment lock before calling it.
A failure while the library itself reads the checkout, decrypts the bundle or parses
its members raises only `BundleError`. Every other error from those steps is replaced
by one fixed message and is not chained, because parser, archive and decryption errors
can quote or reference private values.
Exceptions raised by the four callbacks propagate unchanged. Each integration must
keep private values out of its own errors and logs.
The trusted renderer receives the plan and read-only frozen bytes. It must use the
bundled local charts, values and rendering context, verify its running toolchain,
and return exact rendered bytes per release. Use private scratch storage for tools
that require paths and remove it afterward. The library compares every render,
then calls the observer with the cluster, resource identities and rollback
revisions. It compares the entire selection to the approved observation before
the first write. Before each stack, it compares that stack's projection again.
If both stacks reference a resource that OCI deliberately changed, stop and
review a fresh OTel plan for that change.

The runner calls `reconcile(row, method, files)` in prerequisite order with the
local chart, values and render for that release. Use native Helmfile `apply` for
deployed releases. A failed release gets `sync` even when the diff is empty.
An unexpected absence or pending operation blocks all selected releases.
Since Helmfile v1.8.4, `apply` itself also upgrades a release whose latest
revision is not `deployed`, even with an empty diff. The explicit `sync` and the
pending-state check stay, because they decide before Helmfile runs. To learn the
status, `apply` now runs `helm list` once for each release it diffs, which reads
every Helm release Secret in that namespace. Count that read in the access review
of the apply identity.
`verify_stack(stack)` must return true after the required checks pass. OCI
verification also gates an OTel-only run. A healthy no-op still runs the stack
checks. Keep the lock through verification and record last-known-good only on
successful completion. Kubernetes actors outside that lock can still change
objects. Enforce resource preconditions at each mutation boundary.

Pass `functools.partial(helmfile_transaction.reconcile, kubeconfig=path, context=name)`
to the consumer. It sends the six existing application releases to
`reconcile_existing` and the four companion releases to `reconcile_companion`.
A bundle holds one kind or the other. Start the trusted process with `scripts/demo/helmfile_tools.py run`
from the approved checkout. The launcher verifies the tool archives and supplies
the isolated Helm, Helmfile, helm-diff and post-renderer directory. Prefill
the locked tool cache when the runtime has no public download access.

The callback writes one release state and the sealed inputs in an owner-only
temporary directory. It declares no repositories, skips dependency preparation
and passes the local values file directly to Helm and helm-diff. This preserves
literal values that Helmfile itself could interpret as external references.
Both commands require the frozen post-renderer. Helmfile prepends
`--install` to its upgrade command, so the callback adds `--install=false` to
the final upgrade arguments and `--allow-unreleased=false` to the diff. A
deleted release needs operator review. The caller must still hold the writer lock and reobserve
live state. Helm has no compare-and-swap flag for a release deleted and recreated
by another actor between observation and upgrade.

`reconcile_companion` owns the companion releases. An installed companion uses the
same guarded Helmfile upgrade as an application release. The first install is a
frozen `install` operation with `atomic` false, and the callback accepts it only
for the `apply` method. It runs the pinned Helm binary directly with the frozen
chart, values and post-renderer, because `helm upgrade --install` cannot require
an absent release. `helm install` fails on any stored record of that name that
Helm can read. `consume` has already observed the release as absent under the
lock. The callback never passes `--take-ownership`, so Helm refuses an existing
object that lacks this release's ownership metadata. Apply the reviewed
[ownership patches](helmfile-companions.md#inspect-existing-ownership) first.
A name in use, a rejected render and an ownership conflict all stop before Helm
stores a release record. A failure after that point, such as a wait timeout,
leaves failed revision 1. The observer has no successful rollback target for it
and refuses the release, so this needs operator recovery. Stop automation and
follow [De-adopt companion releases](cutover.md#de-adopt-companion-releases):
remove only the stored release records, and the recorded ownership metadata when
rolling back. Never use `helm uninstall` on a companion release. Then review a
fresh install plan. The kind rehearsal constructs a failed revision 1, removes
only its stored release record with every object unchanged, and installs again
through the callback.

The callback discards both subprocess output streams, including failure output.
It uses the exact trusted kubeconfig and context passed by the runtime and refuses
an empty or unnamed context, which Helm would replace with the kubeconfig's current one. The
Helm subprocesses have a fresh `HOME` and no inherited OCI settings, signing
keys or proxy environment. A kubeconfig exec credential plugin must be present
on the pinned process's trusted `PATH` and authenticate with that environment.
Use a runtime-provided identity or reviewed absolute credential/configuration
paths accessible only to the executor. The local fixture uses a temporary
loopback identity. OKE authentication requires its own rehearsal.

Run the [frozen transaction rehearsal](../scripts/demo/rehearsals/frozen-transaction/exercise.py)
against a fresh loopback Kubernetes fixture using the verified tool launcher:

```sh
python3 scripts/demo/helmfile_tools.py run -- python3 \
  scripts/demo/rehearsals/frozen-transaction/exercise.py \
  --kubeconfig "$FIXTURE_KUBECONFIG" --context fixture
```

The fixture starts with an existing release and a packaged dependency whose
repository is unreachable. It verifies unchanged revision and resources after
guard rejection, a healthy no-op, a Secret-only change under suppressed diff
output, an empty-diff failed release retry with `sync` and with `apply`, the
configured history limit and refusal to reinstall a deleted release.

The [phase rehearsal](../scripts/demo/rehearsals/release-phases/exercise.py)
uses two synthetic Helm releases on disposable Kubernetes. It checks a failed
release with an empty diff, pending-state rejection before writes, zero new
revisions on a healthy no-op and failed OCI verification before OTel.

For each Helm transaction, the consumer derives `postrender.yaml` from the frozen
bundle with `transaction_render`. Write those bytes to a private temporary file and point
`JAEGER_APPROVED_MANIFEST` to that file. Invoke the pinned Helm binary with
`--post-renderer verify-frozen-manifests` so its actual templated resources and
hooks pass the comparison before it starts the transaction. Use a plain upgrade
for an existing release. The full render still includes the archive's native
CRDs. The helper matches each CRD to its exact packaged source, identity and
complete definition, and retains template-owned CRDs in the transaction check.
A new parent or subchart CRD missing from the complete render requires review.

On monitoring upgrades, the reviewed hook Job unpacks a ConfigMap and reapplies
the CRDs. The verifier checks its two container commands, shared volume path and
all decoded CRD definitions against the chart. Recheck the identities, UIDs and
schemas of the live CRDs before and after the hook. Helm installs native CRDs
before calling its post-renderer on first install. The bundle refuses first
installs of charts that contain native CRDs. The supported first installs are
namespace-scoped companion releases.

The bundle library, existing-release transaction callback and synthetic rehearsals
are available for integrating the read-only planner and the restricted executor.
Production authentication, live observation, stack health checks, artifact
transport and key custody remain the responsibility of those integrations.
The credential-free monitoring rehearsal sealed a chart and
139 rendered objects including ten CRDs and three Secret objects. It reproduced
the full render, compared the 129 templated objects and hooks in the real Helm
post-renderer, and rejected a later revision drift in the local test executor.

Reproduce the test with a clean checkout containing the Helmfile, the toolchain
lock and this library. Supply the reviewed monitoring chart package:

```sh
python3 scripts/demo/helmfile_tools.py run -- python3 \
  scripts/demo/rehearsals/adoption-bundle/monitoring.py "$PROMETHEUS_CHART_ARCHIVE"
```

The rehearsal renders on the client, uses a synthetic observer, generates an
ephemeral encryption key, and removes its working files when the test exits.

## Acquiring the live baseline

Use the read-only observer with the reviewed Kubernetes context. During planning,
provide a JSON object with three arrays. `releases` contains the resolved release
IDs, `objects` contains the exact rendered identities and operational guard
objects, and `imageStacks` contains `oci` or `otel` for each selected Jaeger
application. An identity has the form `apiVersion:Kind:namespace:name`, with
`-` for a cluster-scoped object. The observer resolves the REST resource and its
scope through server discovery. The capabilities file contains the Kubernetes
version and the same group/version and kind set used by Helm. A failure to
discover any advertised group stops the observation.

```sh
umask 077
python3 scripts/demo/live_observation.py --scope "$PRIVATE/scope.json" \
  --kubeconfig "$KUBECONFIG" --context "$CONTEXT" \
  --render-context "$PRIVATE/render-context.json" > "$PRIVATE/live.json"
```

Check the command's exit status before reading either output. Use the baseline’s
`cluster` (the `kube-system` Namespace UID) and `rollbackTargets` when sealing
the bundle. The apply adapter is `live_observation.live_reader(kubeconfig, context)`.
Construct it under the same cross-writer lock used for apply. Its input from
`adoption_bundle.consume` includes the expected cluster UID and named rollback
revisions. Drift or a failed health/read check stops consumption. The API serves
each resource independently. External changes concurrent with the final reads
can still occur outside the deployment lock.

The observer retains full object bodies in process memory. The output contains
a UID and SHA-256 for each named object, or JSON null when the GET returned 404.
The hash includes defaulted specifications, data, labels, annotations, ownership,
deletion timestamps and Service/PVC bindings. It excludes top-level status and
the metadata fields resourceVersion, managedFields, generation and creationTimestamp.
Access errors stop collection. Per-stack health calls independently check public
routes, fresh traces and finite SPM. For each selected Jaeger release, the public UI
must serve the tracking settings from its successful Helm record. After a failed
head the observer uses the most recent successful revision. The existing
`recordSHA256` binds these settings to the approved observation. A stack whose
Jaeger release is outside the selection uses the checkout's UI configuration.
Each selected Jaeger and HotROD reference
must match the named Deployment, the active Ready Pods and a registry-verified
Linux AMD64 digest or index. The observer confirms Pod ownership through the
ReplicaSet and Deployment UIDs.

For each Helm release, a label-filtered, metadata-only query discovers revision
identities. The observer GETs the latest Secret and each intervening revision
down to the highest deployed or superseded release, the target Helm uses for
atomic recovery. Each record must agree with its Secret name, namespace, owner,
revision label and status. The observer hashes the **complete decoded stored
release**, including chart files, values, manifests and hook definitions. A
pending latest revision, missing intervening revision, changing ledger, or
different requested target stops acquisition. The first install of a companion
release can observe empty history and a null rollback target.

The reader needs API discovery, a named GET of the `kube-system` Namespace,
and named GET access for every selected object. It also needs Secret list access
per release namespace and GET access on the exact Helm history Secret names.
For Jaeger applications, grant GET on the two named Deployments, list on Pods
and GET on their controlling ReplicaSets. Allow outbound reads to the public
registry, its source tag service and the hosted health endpoints. Kubernetes
RBAC permits a holder of Secret list access to request full Secret values. The
collector limits its own list requests to PartialObjectMetadataList and its
individual GETs to the named scope. This distinction belongs in the production
access review.

PR CI runs the native frozen-transaction rehearsal on its disposable kind node. The test exercises an approved Secret-only change, no-op apply, a failed equal-render release, changed-input rejection and refusal to reinstall an absent release.

The private Helm history fixture accepts `--verify-live-observation`. It uses
a separate read-only identity and real Helm release Secrets on the loopback
API server. It verifies status churn, field and Secret value drift, missing
objects, failed Helm revisions, intervening gaps and rollback selection. The
fixture injects its own health callback. The test sets a Pod status on the private API and verifies its Deployment
ownership chain against synthetic registry digests. [Local fixture instructions](../scripts/demo/rehearsals/helm-history/README.md)
