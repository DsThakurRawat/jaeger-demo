<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Private Helmfile deployment bundles

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
| `releases/<namespace>/<name>/chart.tgz` | Complete chart archive including its packaged dependencies. The plan records the chart source, name, version and SHA-256. |
| `releases/<namespace>/<name>/values.yaml` | Frozen merged user values, including previously supplied file contents, image inputs and chart flags. |
| `releases/<namespace>/<name>/rendered.yaml` | Exact output from the pinned renderer using these inputs and the recorded capabilities. Preserve byte order and encoding. |

The plan's top-level fields are `format`, `configurationSHA`, `mode`, `selector`,
`requested`, `releases`, `renderContext` and `images`. The selector uses `stack`
and `component`. Each release entry has an `id` of `namespace/name`, ordered
`needs`, `chart`, explicit `helm` parameters, `observe`, `checks` and `rollback`.
The application dependency graph and chart pins come from the reviewed Git tree.
For a locally packaged Jaeger chart, use its upstream repository URL followed by
`@` and the exact pinned commit. A companion adoption uses
`component=demo-resources` and the source `charts/demo-resources`. The checker
compares its rendered resource identities and keep annotations against the chart's
committed allowlist.

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
digest, safe archive structure, source tree, selection, tool and chart pins and
recorded recovery targets. Neither CLI command deploys or exports decrypted files.

The live runner entry point is `consume(path, cipher, approved, checkout, expected,
render_frozen, read_live, apply)`. Acquire the deployment lock before calling it.
The trusted renderer receives the plan and read-only frozen bytes. It must use the
bundled local charts, values and rendering context, verify its running toolchain,
and return exact rendered bytes per release. Use private scratch storage for tools
that require paths and remove it afterward. The library compares every render,
then calls the observer with the cluster and resource identities, including
specific rollback revisions. A fresh matching projection is required before the
executor receives any inputs. The executor uses the same frozen charts, values
and Helm parameters, performs OCI verification before any OTel write, and applies
resource preconditions at mutation boundaries. Other actors can change Kubernetes
objects between a read and a write.

The bundle library and synthetic rehearsals are available for integrating the
read-only planner and the restricted executor. Production observation, the actual
Helm transaction, artifact transport and key custody remain the responsibility of
those integrations. The credential-free monitoring rehearsal sealed a chart and
139 rendered objects including ten CRDs and three Secret objects, reproduced its rendered
bytes, handed them to a local test executor and rejected a later revision drift.

Reproduce the test with a clean checkout containing the Helmfile, the toolchain
lock and this library. Supply the reviewed monitoring chart package:

```sh
python3 scripts/demo/helmfile_tools.py run -- python3 \
  scripts/demo/rehearsals/adoption-bundle/monitoring.py "$PROMETHEUS_CHART_ARCHIVE"
```

The rehearsal renders on the client, uses a synthetic observer, generates an
ephemeral encryption key, and removes its working files when the test exits.
