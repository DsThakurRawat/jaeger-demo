#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Authenticated private PVC storage for the scripts-v1 daily deployment."""

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import sys
import zipfile

from cryptography.exceptions import InvalidSignature, InvalidTag
from cryptography.hazmat.primitives import hashes, hpke, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from scheduled_request import daily_slot, format_utc, receipt_fields, timestamp

FORMAT = "jaeger-demo/scripts-v1/private-bundle/v1"
RELEASES = ("oci-jaeger", "oci-monitoring", "opensearch", "opensearch-dashboards", "otel-jaeger", "otel-demo")
MEMBERS = frozenset(
    ["plan.json"] + [f"{kind}/{release}.{suffix}" for release in RELEASES
                     for kind, suffix in (("charts", "tgz"), ("values", "json"), ("renders", "json"))]
    + [f"supplemental/{release}.json" for release in RELEASES if release != "opensearch-dashboards"]
)
MAX_MEMBER = 32 * 1024 * 1024
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_BLOB = MAX_ARCHIVE + 48  # X25519 encapsulated key + AES-GCM tag.
MAX_STORED = 32
SUITE = hpke.Suite(hpke.KEM.X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.AES_256_GCM)


class CustodyError(ValueError):
    pass


class SlotNotFound(CustodyError):
    """The store and verification key exist, but the requested slot has no signed record."""


def require(condition, message):
    if not condition:
        raise CustodyError(message)


def canonical(obj):
    return json.dumps(obj, separators=(",", ":"), sort_keys=True, allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def slot_name(slot):
    t = timestamp(slot)
    require(slot == format_utc(daily_slot(t)), "Expected an exact 13:00 UTC daily slot")
    return slot[:10] + ".slot"


def read_fd(fd, limit):
    with os.fdopen(fd, "rb") as stream:
        meta = os.fstat(fd)
        require(stat.S_ISREG(meta.st_mode) and 0 < meta.st_size <= limit, "Invalid custody file type or size")
        data = stream.read(limit + 1)
        require(len(data) == meta.st_size, "Custody file changed during read")
        return data


def read_at(directory, name, limit):
    return read_fd(os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory), limit)


@contextmanager
def open_store(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        require(not os.fstat(fd).st_mode & 0o007, "Custody directory must exclude other users")
        yield fd
    finally:
        os.close(fd)


def key_file(path, kind, private=False):
    # Kubernetes Secret projections link through ..data; follow only into this trusted mount.
    path = Path(path)
    resolved = path.resolve(strict=True)
    require(resolved.is_relative_to(path.parent.resolve(strict=True)), "Key link leaves its mounted directory")
    fd = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        require(not os.fstat(fd).st_mode & 0o022, "Custody credentials must exclude group and other writers")
        if private:
            require(not os.fstat(fd).st_mode & 0o037, "Private credential must exclude other users and group writes")
        data = read_fd(os.dup(fd), 4096)
    finally:
        os.close(fd)
    try:
        value = (serialization.load_pem_private_key(data, None) if private else serialization.load_pem_public_key(data))
    except (ValueError, TypeError):
        raise CustodyError("Invalid PEM custody credential") from None
    require(isinstance(value, kind), "Incorrect custody credential type")
    return value


def pack(files):
    require(set(files) == MEMBERS, "Incomplete or unexpected private bundle members")
    require(all(isinstance(b, bytes) and 0 < len(b) <= MAX_MEMBER for b in files.values())
            and sum(map(len, files.values())) <= MAX_ARCHIVE, "Invalid private member type or size")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", allowZip64=False) as target:
        for name in sorted(MEMBERS):
            entry = zipfile.ZipInfo(name)  # Fixed timestamp and order make equal inputs reproducible.
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o600) << 16
            target.writestr(entry, files[name])
    require(buf.tell() <= MAX_ARCHIVE, "Private bundle exceeds capacity")
    return buf.getvalue()


def unpack(data):
    # Validate the record count before ZipFile allocates central directory objects.
    require(22 <= len(data) <= MAX_ARCHIVE and data[-22:-18] == b"PK\x05\x06", "Invalid private archive")
    disk, directory, count, total, size, offset, comment = struct.unpack("<4H2LH", data[-18:])
    require(disk == directory == comment == 0 and count == total == len(MEMBERS)
            and size <= 256 * total and offset + size == len(data) - 22, "Invalid private archive directory")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            require(len(entries) == total and {i.filename for i in entries} == MEMBERS, "Unexpected private archive members")
            require(all(i.compress_type == zipfile.ZIP_STORED and i.flag_bits == 0
                        and stat.S_ISREG(i.external_attr >> 16) and 0 < i.file_size == i.compress_size <= MAX_MEMBER
                        for i in entries) and sum(i.file_size for i in entries) <= MAX_ARCHIVE,
                    "Unsafe private archive entry")
            return {entry.filename: archive.read(entry) for entry in entries}
    except (zipfile.BadZipFile, EOFError, ValueError) as error:
        if isinstance(error, CustodyError):
            raise
        raise CustodyError("Invalid private archive contents") from None


def context(slot, sha, recipient):
    slot_name(slot)
    require(isinstance(sha, str) and re.fullmatch("[0-9a-f]{40}", sha), "Expected reviewed configuration SHA")
    return {"format": FORMAT, "slot": slot, "configurationSHA": sha, "recipient": digest(recipient.public_bytes_raw())}


def receipt(claim):
    return receipt_fields({name: claim[name] for name in ("configurationSHA", "bundleSHA256")})


def signed_slot(fd, slot, verifier):
    try:
        raw = read_at(fd, slot_name(slot), 2048)
    except FileNotFoundError:
        raise SlotNotFound("Daily bundle has not been published") from None
    try:
        stored = json.loads(raw)
        claim = stored["claim"]
        # Canonical encoding also rejects duplicate JSON properties or extra outer fields.
        signature = bytes.fromhex(stored["signature"])
        require(raw == canonical({"claim": claim, "signature": signature.hex()}), "Invalid signed slot encoding")
        verifier.verify(signature, canonical(claim))
        require(set(claim) == {"format", "slot", "configurationSHA", "recipient", "inputCommitment", "bundleSHA256"}
                and claim["format"] == FORMAT and claim["slot"] == slot
                and all(re.fullmatch("[0-9a-f]{64}", claim[k]) for k in ("recipient", "inputCommitment")),
                "Signed slot scope differs")
        receipt(claim)
        return claim
    except (InvalidSignature, ValueError, TypeError, KeyError) as error:
        if isinstance(error, CustodyError):
            raise
        raise CustodyError("Invalid signed daily slot") from None


def blob(fd, claim):
    data = read_at(fd, claim["bundleSHA256"] + ".hpke", MAX_BLOB)
    require(digest(data) == claim["bundleSHA256"], "Stored ciphertext digest mismatch")
    return data


def trusted_receipt(store, slot, *, verification_key):
    verifier = key_file(verification_key, ed25519.Ed25519PublicKey)
    with open_store(store) as fd:
        claim = signed_slot(fd, slot, verifier)
        blob(fd, claim)  # An incomplete/corrupt store is never treated as an unpublished slot.
        return receipt(claim)


def write_once(fd, name, content):
    temp = ".pending-" + secrets.token_hex(16)
    file = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fd)
    try:
        with os.fdopen(file, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o440)
            os.fsync(stream.fileno())
        os.link(temp, name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        os.fsync(fd)
    finally:
        os.unlink(temp, dir_fd=fd)


def publish(store, slot, configuration_sha, files, *, signing_key, recipient_key, before_index=None):
    signer = key_file(signing_key, ed25519.Ed25519PrivateKey, private=True)
    recipient = key_file(recipient_key, x25519.X25519PublicKey)
    info = context(slot, configuration_sha, recipient)
    private = pack(files)
    # A keyed commitment permits exact retries without exposing a plaintext digest for offline guessing.
    mac = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=(FORMAT + "/input-commitment").encode()).derive(signer.private_bytes_raw())
    commitment = hmac.digest(mac, private, "sha256").hex()
    with open_store(store) as fd:
        # The existing PVC directory is the common, stable lock inode for all preparers.
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            previous = signed_slot(fd, slot, signer.public_key())
        except SlotNotFound:
            previous = None
        if previous is not None:
            require(previous["inputCommitment"] == commitment and all(previous[k] == v for k, v in info.items()),
                    "Another immutable plan already owns this daily slot")
            blob(fd, previous)
            return receipt(previous)
        # Orphans and interrupted writes also count. No automatic eviction of recovery evidence.
        with os.scandir(fd) as entries:
            slots = blobs = 0
            for entry in entries:
                slots += entry.name.endswith(".slot")
                blobs += entry.name.endswith(".hpke") or entry.name.startswith(".pending-")
        require(max(slots, blobs) < MAX_STORED, "Bundle store is full; review retained slots and interrupted writes")
        encrypted = SUITE.encrypt(private, recipient, canonical(info))
        claim = {**info, "inputCommitment": commitment, "bundleSHA256": digest(encrypted)}
        write_once(fd, claim["bundleSHA256"] + ".hpke", encrypted)
        # Once the dated index is visible, the preparer can resume without decrypting or
        # re-resolving. Finish independent in-cluster publication before that commit point.
        if before_index is not None:
            before_index(receipt(claim))
        signed = canonical({"claim": claim, "signature": signer.sign(canonical(claim)).hex()})
        write_once(fd, slot_name(slot), signed)
        return receipt(claim)


def load(store, slot, receipt, *, verification_key, decryption_key):
    expected = receipt_fields(receipt)
    verifier = key_file(verification_key, ed25519.Ed25519PublicKey)
    recipient = key_file(decryption_key, x25519.X25519PrivateKey, private=True)
    info = context(slot, expected["configurationSHA"], recipient.public_key())
    with open_store(store) as fd:
        claim = signed_slot(fd, slot, verifier)
        require(all(claim[k] == v for k, v in (expected | info).items()), "Bundle differs from independently approved slot")
        encrypted = blob(fd, claim)
    try:
        private = SUITE.decrypt(encrypted, recipient, canonical(info))
    except (ValueError, InvalidTag):
        raise CustodyError("Private bundle authentication failed") from None
    return unpack(private)


def generate_keys(destination):
    # Operator-only. Refuse to reuse a directory or overwrite previous recovery material.
    root = Path(destination)
    root.mkdir(mode=0o700)
    signer, recipient = ed25519.Ed25519PrivateKey.generate(), x25519.X25519PrivateKey.generate()
    for name, key in (("signing-key.pem", signer), ("verification-key.pem", signer.public_key()),
                      ("decryption-key.pem", recipient), ("recipient-public.pem", recipient.public_key())):
        if name in ("signing-key.pem", "decryption-key.pem"):
            data = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
        else:
            data = key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        with os.fdopen(os.open(root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("generate-keys", help="Create new local operator-held PEM keys").add_argument("new_directory", type=Path)
    args = parser.parse_args()
    try:
        generate_keys(args.new_directory)
    except (OSError, ValueError):
        sys.exit("Key generation failed; use a new owner-only destination and check the filesystem")
