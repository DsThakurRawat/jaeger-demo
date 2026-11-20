# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Select OpenSearch recovery behavior for the workflow and standalone scripts."""

import os
import sys


def resolve(env, stack, mode, scope, requested):
    event = env.get("GITHUB_EVENT_NAME")
    main = env.get("GITHUB_REF") == "refs/heads/main"
    if requested == "waive":
        if event == "workflow_dispatch" and main and (stack, mode, scope) == ("otel", "upgrade", "opensearch"):
            return "waive"
        raise ValueError("OpenSearch recovery may be waived only by a manual isolated otel/upgrade/opensearch run from main")
    if requested not in ("", "required"):
        raise ValueError(f"Invalid OpenSearch recovery policy '{requested}'")
    if event == "schedule":
        if not main or mode != "upgrade" or requested:
            raise ValueError("Scheduled OpenSearch updates require main, upgrade mode and no recovery override")
        return "preserve-pvc"
    return "required"


if __name__ == "__main__":
    try:
        print(resolve(os.environ, *sys.argv[1:]))
    except ValueError as error:
        sys.exit(str(error))
