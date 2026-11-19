#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Pass Helm's combined resource and hook stream only if it matches the approved render."""

import json
import os
from pathlib import Path
import sys

import yaml


FILENAME = "postrenderer.helm.sh/postrender-filename"
LIMIT = 64 * 1024 * 1024


class ManifestLoader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    pass


ManifestLoader.add_constructor("tag:yaml.org,2002:value", ManifestLoader.construct_scalar)


def read(stream):
    data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError("Manifest stream exceeds size limit")
    return data


def objects(raw, helm_stream):
    by_id = {}
    for obj in yaml.load_all(raw, Loader=ManifestLoader):
        if obj is None:
            continue
        meta = obj["metadata"]
        if helm_stream:
            # Helm 4 annotates individual documents before running a post-renderer.
            annotations = meta.get("annotations", {})
            if FILENAME not in annotations:
                raise ValueError("Helm filename annotation missing")
            del annotations[FILENAME]
            if not annotations:
                meta.pop("annotations", None)
        elif FILENAME in (meta.get("annotations") or {}):
            raise ValueError("Approved render contains a reserved Helm annotation")
        if not meta.get("annotations"):
            meta.pop("annotations", None)
        identity = (obj["apiVersion"], obj["kind"], meta.get("namespace", ""), meta["name"])
        if identity in by_id:
            raise ValueError("Duplicate manifest identity")
        # JSON encoding preserves string/number and boolean/number distinctions.
        by_id[identity] = json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if not by_id:
        raise ValueError("Empty manifest stream")
    return by_id


def main():
    raw = read(sys.stdin.buffer)
    with Path(os.environ["JAEGER_APPROVED_MANIFEST"]).open("rb") as src:
        expected = objects(read(src), helm_stream=False)
    if objects(raw, helm_stream=True) != expected:
        raise ValueError("Render differs from the approved release")
    sys.stdout.buffer.write(raw)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, AttributeError, KeyError, yaml.YAMLError):
        # Parser messages can include the raw manifest, including Secret contents.
        sys.exit("Frozen Helm manifest verification failed")
