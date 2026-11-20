# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""The fixture Pods use one deterministic daily slot regardless of CI start time."""

from pathlib import Path
import scheduled_request


def fixture_now():
    return scheduled_request.timestamp(Path("/fixture/slot").read_text().strip())


scheduled_request.utcnow = fixture_now
