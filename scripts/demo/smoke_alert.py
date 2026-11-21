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
* A run that was rerun, or whose last attempt was cancelled, still has an
  observed result: the latest ``success``/``failure`` among its earlier
  attempts, found with ``gh api .../attempts/N``. The run being rerun right now
  is not completed, so its earlier attempts count the same way. All results are
  ordered by the time they were observed (``updated_at``), newest first, so a
  rerun of an old run cannot override a newer run. If any of these lookups
  fails the whole history counts as unreadable (see above), because skipping an
  attempt could surface a stale result.
* A cancelled run is silent. It proves nothing about the demos, and the next
  run checks again within 30 minutes.

Any other event (``workflow_dispatch``) is a person asking for a result. A
failure or a cancellation always posts ``failed`` and ``recovered`` is never
posted.
"""

import argparse
import json
import os
import subprocess
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


def read_runs(path):
    """Read the run list from ``gh api .../runs`` output, or ``None`` if unusable."""
    try:
        with open(path, encoding='utf-8') as handle:
            runs = json.load(handle)['workflow_runs']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return runs if isinstance(runs, list) else None


def fetch_attempt(run_id, attempt):
    """Return one attempt of a workflow run from the GitHub API."""
    repo = os.environ['GITHUB_REPOSITORY']
    result = subprocess.run(['gh', 'api', f'repos/{repo}/actions/runs/{run_id}/attempts/{attempt}'],
                            capture_output=True, text=True, check=True, timeout=15)
    return json.loads(result.stdout)


def latest_conclusive(run_id, attempt, fetch):
    """Return ``(conclusion, observed_at)`` of the newest conclusive attempt up to ``attempt``.

    A failed lookup raises. Skipping an unreadable attempt could surface an
    older, stale result in its place.
    """
    for number in range(attempt, 0, -1):
        data = fetch(run_id, number)
        if not isinstance(data, dict):
            raise ValueError(f'unexpected attempt {number} of run {run_id}')
        conclusion = data.get('conclusion')
        if conclusion in ('success', 'failure'):
            return conclusion, str(data.get('updated_at') or '')
    return None


def build_history(runs, run_id=None, run_attempt=1, fetch=fetch_attempt):
    """Return the conclusive results of earlier runs, newest first.

    ``runs`` is the completed run list (newest first) or ``None``. The current
    run is left out of it, but its own earlier attempts count when it is a
    rerun. Results are ordered by when they were observed; ties and missing
    timestamps keep the list order. Raises when an attempt lookup fails.
    """
    found = []
    for run in runs or ():
        if not isinstance(run, dict) or str(run.get('id')) == str(run_id):
            continue
        conclusion = run.get('conclusion')
        if conclusion in ('success', 'failure'):
            found.append((conclusion, str(run.get('updated_at') or '')))
        elif isinstance(run.get('run_attempt'), int) and run['run_attempt'] > 1:
            earlier = latest_conclusive(run.get('id'), run['run_attempt'] - 1, fetch)
            if earlier:
                found.append(earlier)
    if run_id and run_attempt > 1:
        earlier = latest_conclusive(run_id, run_attempt - 1, fetch)
        if earlier:
            found.append(earlier)
    found.sort(key=lambda item: item[1], reverse=True)
    return [conclusion for conclusion, _ in found]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--result', required=True, choices=RESULTS, help='conclusion of this run so far')
    parser.add_argument('--event', required=True, help='triggering event name')
    parser.add_argument('--history', help='JSON from the workflow runs API, newest run first')
    parser.add_argument('--run-id', help='id of this run, excluded from the history')
    parser.add_argument('--run-attempt', type=int, default=1, help='attempt number of this run')
    args = parser.parse_args(argv)
    runs = read_runs(args.history) if args.history else None
    try:
        history = build_history(runs, args.run_id, args.run_attempt)
    except Exception as error:  # fail open: an unreadable history is treated as no history
        print(f'warning: could not read earlier attempts: {error}', file=sys.stderr)
        history = None
    print(decide(args.result, args.event, history))
    return 0


if __name__ == '__main__':
    sys.exit(main())
