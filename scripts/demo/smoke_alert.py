#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Decide whether the scheduled smoke check posts to Slack.

The workflow retries a failed stack check, then asks this helper what to post:

* ``failed``: the check failed and people should hear about it.
* ``recovered``: the check passes again after a failing scheduled run.
* ``none``: stay silent.

Rules for scheduled runs, which deduplicate on the state change:

* A failure posts ``failed`` only when the most recent earlier scheduled run
  with a ``success`` or ``failure`` conclusion did not fail. Other conclusions
  (``cancelled``, ``skipped``, ``timed_out`` and the rest) say nothing about
  health and are skipped when looking back.
* A success posts ``recovered`` only when that earlier run failed.
* With no readable history or no usable conclusion, a failure posts (fail
  open, so a broken lookup never hides a real outage) and a success is silent.
* A cancelled run is silent. It proves nothing about the demos, and the next
  run checks again within 30 minutes.

Any other event (``workflow_dispatch``) is a person asking for a result. A
failure or a cancellation always posts ``failed`` and ``recovered`` is never
posted.
"""

import argparse
import json
import sys


FAILED = 'failed'
RECOVERED = 'recovered'
NONE = 'none'
RESULTS = ('success', 'failure', 'cancelled')
SCHEDULE = 'schedule'


def previous_state(history):
    """Return the newest usable conclusion in ``history`` or ``None``."""
    for conclusion in history or ():
        if conclusion in ('success', 'failure'):
            return conclusion
    return None


def decide(result, event, history):
    """Return ``failed``, ``recovered`` or ``none``.

    ``history`` holds the conclusions of earlier completed scheduled runs,
    newest first, without the current run. ``None`` means it could not be read.
    """
    if result not in RESULTS:
        raise ValueError(f'unknown result: {result!r}')
    if event != SCHEDULE:
        return FAILED if result in ('failure', 'cancelled') else NONE
    if result == 'cancelled':
        return NONE
    previous = previous_state(history)
    if result == 'failure':
        return NONE if previous == 'failure' else FAILED
    return RECOVERED if previous == 'failure' else NONE


def read_history(path, current_run_id=None):
    """Read conclusions from ``gh api .../runs`` output, or ``None`` if unusable."""
    try:
        with open(path, encoding='utf-8') as handle:
            runs = json.load(handle)['workflow_runs']
        if not isinstance(runs, list):
            return None
        return [run.get('conclusion') for run in runs
                if isinstance(run, dict) and str(run.get('id')) != str(current_run_id)]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--result', required=True, choices=RESULTS, help='conclusion of this run so far')
    parser.add_argument('--event', required=True, help='triggering event name')
    parser.add_argument('--history', help='JSON from the workflow runs API, newest run first')
    parser.add_argument('--run-id', help='id of this run, excluded from the history')
    args = parser.parse_args(argv)
    history = read_history(args.history, args.run_id) if args.history else None
    print(decide(args.result, args.event, history))
    return 0


if __name__ == '__main__':
    sys.exit(main())
