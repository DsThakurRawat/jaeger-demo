#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Write a private kubeconfig that sends every request as a deployer identity.

The operator runbook (docs/cutover.md#helmfile-adoption) runs its Cloud Shell steps with the
cluster administrator's kubeconfig, impersonating the manual deployer identity, so that each
step has exactly the access of ops/rbac/deployer.yaml. The output holds one context, the given
one, as its current context, with its cluster and its user. The user entry keeps the
administrator's credential and gains `as` (and `as-groups` when groups are given); the API server
checks that the administrator may impersonate, then authorizes the request as the deployer.
The file is created with mode 0600 and never overwritten. It names the impersonated OCID and the
cluster endpoint, so keep it out of any record that is copied or kept, and delete it after use.
"""

import argparse
import os
from pathlib import Path
import sys

import yaml

from deployer_access import USER

# Kubeconfig fields that name a file. kubectl resolves a relative one against the directory of the
# kubeconfig that holds it, so the copy, written elsewhere, makes them absolute.
CLUSTER_FILES = ("certificate-authority",)
USER_FILES = ("client-certificate", "client-key", "tokenFile")
IMPERSONATION = ("as", "as-uid", "as-groups", "as-user-extra")


def absolute(path, base):
    return path if not path or os.path.isabs(path) else str((base / path).resolve())


def named(entries, name, kind):
    found = [entry for entry in entries or [] if entry.get("name") == name]
    if len(found) != 1:
        raise ValueError(f"The kubeconfig has {len(found)} {kind} entries named {name!r}, not one")
    return found[0]


def impersonating(config, context, user, groups=(), base=Path(".")):
    """Return a kubeconfig with only `context`, whose user entry impersonates `user`."""
    if not USER.fullmatch(user or ""):
        raise ValueError("The deployer identity must be an OCI user OCID (ocid1.user.oc1..)")
    # `oci ce cluster create-kubeconfig` writes `kind: ""`, which kubectl accepts like an absent kind.
    if not isinstance(config, dict) or config.get("kind") not in (None, "", "Config"):
        raise ValueError("Not a kubeconfig")
    selected = named(config.get("contexts"), context, "context")
    details = selected.get("context") or {}
    cluster = named(config.get("clusters"), details.get("cluster"), "cluster")
    credential = named(config.get("users"), details.get("user"), "user")
    entry = dict(credential.get("user") or {})
    present = [key for key in IMPERSONATION if key in entry]
    if present:
        raise ValueError("The user entry already impersonates (" + ", ".join(present) + "); use the administrator's own")
    for key in USER_FILES:
        if key in entry:
            entry[key] = absolute(entry[key], base)
    command = (entry.get("exec") or {}).get("command")
    if command and os.sep in command:
        # kubectl resolves an exec command that contains a path separator against the kubeconfig too.
        entry["exec"] = dict(entry["exec"], command=absolute(command, base))
    entry["as"] = user
    if groups:
        entry["as-groups"] = list(groups)
    server = dict(cluster.get("cluster") or {})
    for key in CLUSTER_FILES:
        if key in server:
            server[key] = absolute(server[key], base)
    return {"apiVersion": "v1", "kind": "Config", "current-context": context,
            "clusters": [{"name": cluster["name"], "cluster": server}],
            "users": [{"name": credential["name"], "user": entry}],
            "contexts": [{"name": context, "context": details}]}


def write(source, context, user, output, groups=()):
    """Write the impersonating copy of the kubeconfig `source` to `output`, mode 0600, never overwriting."""
    source = Path(source)
    if os.pathsep in str(source):
        raise ValueError("Pass a single kubeconfig file")
    try:
        loaded = yaml.safe_load(source.read_text())
    except yaml.YAMLError:
        # The parser's message quotes the input, which holds credentials.
        raise ValueError("The kubeconfig is not valid YAML") from None
    config = impersonating(loaded, context, user, groups, base=source.resolve().parent)
    text = yaml.safe_dump(config, sort_keys=False)
    descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with open(descriptor, "w") as stream:
            stream.write(text)
    except BaseException:
        # Leave no partial kubeconfig behind; the next run would refuse to overwrite it.
        Path(output).unlink(missing_ok=True)
        raise
    return Path(output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kubeconfig", required=True, help="The administrator's kubeconfig file")
    parser.add_argument("--context", required=True, help="The context of the demo cluster in that file")
    parser.add_argument("--user", required=True, help="OCID of the deployer identity's OCI user")
    parser.add_argument("--group", action="append", default=[],
                        help="A group to impersonate as well; repeat as needed. The API server adds system:authenticated")
    parser.add_argument("--output", required=True, help="The kubeconfig to create; it must not exist")
    args = parser.parse_args(argv)
    try:
        path = write(args.kubeconfig, args.context, args.user, args.output, args.group)
    except (ValueError, OSError) as error:
        parser.exit(1, f"error: {error}\n")
    # The OCID stays out of the output, which operators paste into records.
    print(f"Wrote {path}: context {args.context}, every request as the given deployer user")
    return 0


if __name__ == "__main__":
    sys.exit(main())
