<!-- Copyright (c) 2026 The Jaeger Authors. -->
<!-- SPDX-License-Identifier: Apache-2.0 -->

# Daily script bundle custody

The preparer freezes one six-release plan for a specific 13:00 UTC slot and reviewed configuration commit. The receiver reads that plan from a private PVC and checks GitHub's two-field receipt against an independently signed slot record. Release identity, allowed operations, drift, execution order and health checks belong to the script adapter. The private store authenticates the exact bytes the preparer froze. [Request protocol](daily-update-request.md)

| Identity | Bundle PVC at `/var/lib/jaeger-bundles` | Keys at `/var/run/jaeger-bundle-keys` | Output |
| --- | --- | --- | --- |
| Preparer | Read/write | `signing-key.pem`, `recipient-public.pem`, `verification-key.pem` | Immutable ciphertext, signed slot record and a public receipt |
| Receiver | Read-only | `verification-key.pem`, `decryption-key.pem` | Verified inputs in memory and a separate executor-owned outcome record in `/var/lib/jaeger-outcomes` |
| GitHub requester | None | None | Configuration SHA and ciphertext SHA-256 in the request |

Use separate Kubernetes ServiceAccounts and Secret projections. Give both pods a controlled shared filesystem group so the receiver can read mode-0440 bundle files. The custody directory must exclude other users, for example mode 2770 on a pre-provisioned directory on the volume. The preparer requires a filesystem that supports POSIX flock, hard links and durable directory fsync. With a ReadWriteOnce block volume, confirm both pods can mount it on the same node. The receiver mounts the bundles subdirectory read-only and mounts the outcomes subdirectory separately with write access. The preparer has access only to the bundles subdirectory. Configure filesystem quotas with headroom for metadata. Authorize snapshot or backup readers separately because they retain ciphertext and execution metadata.

## Keys and rotation

The operator can create the initial material in a fresh directory on a private workstation:

```sh
python3 scripts/demo/scheduled_bundle.py generate-keys "$NEW_PRIVATE_DIRECTORY"
```

The command requires the repository's pinned Python `cryptography` package. It writes four files in a new mode-0700 directory, with each file mode 0600. It refuses an existing directory and prints no key data. An interrupted generation can leave an incomplete directory. Generate a complete new set in a different directory. Retain only the sets needed for active slots and recovery.

The signer uses Ed25519. The recipient uses X25519. Private keys are unencrypted PKCS#8 PEM, and public keys are SubjectPublicKeyInfo PEM. Import each identity's permitted files into its dedicated key-management destination and Kubernetes Secret through the operator's approved provisioning process. Mount keys read-only. For Kubernetes Secret projections, use group-readable mode 0440 or owner-readable mode 0400 suitable for the pod UID. The library accepts Kubernetes `..data` links confined to that key mount. It rejects private files accessible by other users and keys writable by other users or the group.

For rotation, pause preparation, drain or resolve in-flight slots, and preserve the signing public key and recipient private key needed to read retained bundles. Switch both pods at a known slot boundary. Existing slots require the original verification and decryption keys. Use the retained key set for historical readback. Run a synthetic round-trip against the replacement mounts before releasing a production slot. Key generation, key distribution and backup custody are separate operator actions.

## Prepare and consume

The [custody module](../scripts/demo/scheduled_bundle.py) exports three functions. All slots use the exact timestamp produced by `scheduled_request.format_utc` at 13:00 UTC. The caller supplies key paths and the private PVC path as trusted process configuration.

1. Call `trusted_receipt(store, slot, verification_key=...)` before resolving a new plan. It verifies the signed record and ciphertext digest. Only `SlotNotFound` means a fresh slot can be prepared. A missing store, missing key, unreadable ciphertext or invalid signature requires investigation.
2. Call `publish(store, slot, configuration_sha, files, signing_key=..., recipient_key=...)` with the complete frozen inputs. It returns `{"configurationSHA": "...", "bundleSHA256": "..."}`. An exact retry returns the original receipt. Different inputs, source or recipient for the same slot fail. The publisher uses a locked, write-once link and fsync for the ciphertext before publishing the signed slot. A crash between those writes can leave an unreachable ciphertext.
3. Use the `before_index(receipt)` callback in the preparer to finish publishing independent admission parameters while the frozen inputs are still available in memory. The ciphertext is durable before the callback runs. Failure leaves the slot unpublished and the encrypted orphan subject to retention. An authenticated existing slot skips this callback on retry. Verify its parameters before advertising it without re-resolving or decrypting anything.
4. In the receiver's protected deployment lock, call `load(store, slot, receipt, verification_key=..., decryption_key=...)`. It verifies the receipt and signing key before decrypting. It returns the 24 named files as bytes for the fixed script adapter. The adapter rechecks the live preconditions before any mutation and records the outcome after verification. Keep decrypted files and command output inside the authorized executor.

The encrypted archive contains `plan.json`, six charts, six values files, six renders and five supplemental files. Directories and filenames are fixed by the script adapter. The reader refuses missing or additional members, duplicate paths, links, compressed entries and oversized inputs. The maximum member is 32 MiB. The complete archive is capped at 64 MiB. Plaintext is packed in memory. Only the HPKE ciphertext and signed slot metadata are written to the custody PVC.

HPKE uses X25519, HKDF-SHA256 and AES-256-GCM from the pinned cryptography implementation. Its context binds the protocol and engine, slot, configuration commit and recipient public key. The separate Ed25519 signature authenticates these fields, the SHA-256 of the ciphertext and a keyed commitment used to recognize exact retries. The public receipt exposes only the commit and ciphertext hash. A digest by itself provides no signing authority. [RFC 9180](https://www.rfc-editor.org/rfc/rfc9180) · [Dependency pin](../scripts/demo/oci-cli-constraints.txt)

## Retention and recovery

The preparer refuses a new slot once the PVC holds 32 signed slot files or 32 ciphertext and interrupted staging files. An exact retry of an existing slot continues to work at capacity. This bounds normal payload storage to approximately 2 GiB plus filesystem overhead. Monitor both counts before they reach the limit. The limit preserves evidence when the separate retention process is late.

Perform retention under a separate operator maintenance identity:

1. Pause preparation, drain the receiver, and verify the shared deployment lock and any manual deployment have stopped. Keep the most recent 14 completed daily slots, the current request, every unresolved/failed or verified-but-unfinalized operation, and any artifact referenced by last-known-good, rollback or incident records.
2. Read each older candidate's signed slot and verify its ciphertext digest. Match its slot, configuration and bundle SHA with a terminal durable deployment or recovered-failure record. Confirm the independent audit record and key custody will outlive the bundle. A missing or mismatched record requires operator review.
3. After recording the exact approved deletion list, remove the candidate slot index first. Remove its ciphertext only after verifying that no retained signed index or recovery reference names that digest. On a maintenance crash, deleting the index first leaves an unreachable ciphertext for the next inventory. Age alone does not establish that an object is unused.
4. Inspect old `.pending-*` files and unreferenced ciphertext left by a crashed publisher while both pods are drained. Verify they are absent from every retained index and recovery reference before deletion. Resume both pods and verify readback of the retained last-known-good receipt before reopening scheduling.

The store provides no deletion operation to either application. POSIX write access allows a compromised preparer or storage administrator to remove files. Protect the preparer, PVC administration, signer and independent backup with their own access controls. A missing required bundle blocks execution.

## Local evidence

`scheduled_bundle_test.py` exercises the real crypto and filesystem with temporary keys and synthetic members. It covers concurrent prepares, immutable retries, cross-slot substitution, wrong keys, tampering, read-only access, Kubernetes key projection, bounded archives, a missing ciphertext after publication and capacity exhaustion after an interrupted write. The deployment rehearsal must additionally verify the actual PVC's durability and permissions, distinct pod identities and operator key delivery.
