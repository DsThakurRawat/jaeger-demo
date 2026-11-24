#!/usr/bin/env python3

# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Delete the disposable Jaeger trace indices of the hosted OTel demo and bring Jaeger back.

This is the manual reset in docs/releases.md. It deletes no Kubernetes object.
Every name it touches is a constant below; no argument, input or environment
variable selects a namespace, a Pod, a workload or an index.

`list` prints what a reset would delete and changes nothing. `reset` needs the
exact acknowledgment phrase, then:

1. checks that the one OpenSearch Pod is the expected one,
2. deletes, one exact name per request, the indices that are a daily Jaeger
   trace index and nothing else,
3. restarts the Jaeger Deployment that writes them, so it recreates its index
   templates and forgets the service names it had already recorded, and
4. sends a fresh trace through the collector and reads it back.
"""

import argparse
from contextlib import ExitStack
import json
import os
import re
import subprocess
import sys
import time
import urllib.error

import verify_trace


NAMESPACE = "opensearch"
STATEFULSET = "opensearch-cluster-single"
POD = STATEFULSET + "-0"
CONTAINER = "opensearch"
CLUSTER_NAME = "opensearch-cluster"
JAEGER_NAMESPACE = "jaeger"
JAEGER_DEPLOYMENT = "jaeger"
API = "http://localhost:9200/"
# examples/otel-demo/jaeger-config.yaml: index_prefix "jaeger-main" and one index
# per day for each of these. The archive indices and everything else stay.
FAMILIES = ("span", "service", "dependencies", "sampling")
TRACE_INDEX = re.compile(r"jaeger-main-jaeger-(?:" + "|".join(FAMILIES) + r")-[0-9]{4}-[0-9]{2}-[0-9]{2}")
ACKNOWLEDGMENT = "delete all Jaeger trace indices in " + NAMESPACE + "/" + POD
# The service name verify_trace.check_trace writes its trace under.
CHECK_SERVICE = "jaeger-demo-rollout-check"


class Refused(Exception):
    """The request, or what the cluster holds, is outside what this reset may touch."""


def run(args, limit=180):
    # The limit, in seconds, bounds one command, so a hung API server cannot hold the concurrency group.
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=limit)


def validate_request(operation, acknowledgment, env):
    """Refuse before any cluster call. A schedule, a push or another branch can never reset."""
    if operation not in ("list", "reset"):
        raise Refused("The operation must be list or reset")
    event = env.get("GITHUB_EVENT_NAME", "")
    if event not in ("", "workflow_dispatch"):
        raise Refused("Only a manual dispatch may run this; the event was " + event)
    if event == "workflow_dispatch" and env.get("GITHUB_REF") != "refs/heads/main":
        raise Refused("A dispatch must come from main")
    if operation == "reset" and acknowledgment != ACKNOWLEDGMENT:
        raise Refused('Type the acknowledgment exactly, with no quotes or extra spaces: "%s"' % ACKNOWLEDGMENT)


def opensearch(method, path):
    """One request to the engine from inside its own Pod. The Pod and container are constants."""
    result = run(["kubectl", "-n", NAMESPACE, "exec", POD, "-c", CONTAINER, "--",
                  "curl", "-fsS", "--max-time", "60", "-X", method, API + path])
    return json.loads(result.stdout)


def check_target():
    """The Pod must be the one the OpenSearch StatefulSet controls, and Jaeger must be there to restart."""
    pod = json.loads(run(["kubectl", "-n", NAMESPACE, "get", "pod", POD, "-o", "json"]).stdout)
    metadata, spec = pod.get("metadata", {}), pod.get("spec", {})
    controllers = [(owner.get("kind"), owner.get("name")) for owner in metadata.get("ownerReferences", [])
                   if owner.get("controller")]
    if controllers != [("StatefulSet", STATEFULSET)] or metadata.get("deletionTimestamp"):
        raise Refused("Pod %s/%s is not the live Pod of StatefulSet %s" % (NAMESPACE, POD, STATEFULSET))
    if CONTAINER not in [container.get("name") for container in spec.get("containers", [])]:
        raise Refused("Pod %s/%s has no %s container" % (NAMESPACE, POD, CONTAINER))
    engine = opensearch("GET", "")
    if engine.get("cluster_name") != CLUSTER_NAME:
        raise Refused("The engine in %s/%s is not the %s cluster" % (NAMESPACE, POD, CLUSTER_NAME))
    if opensearch("GET", "_cluster/health").get("number_of_nodes") != 1:
        raise Refused("The %s cluster does not have exactly one node" % CLUSTER_NAME)
    deployment = json.loads(run(["kubectl", "-n", JAEGER_NAMESPACE, "get", "deployment", JAEGER_DEPLOYMENT,
                                 "-o", "json"]).stdout)
    if deployment.get("metadata", {}).get("name") != JAEGER_DEPLOYMENT:
        raise Refused("Deployment %s/%s cannot be read" % (JAEGER_NAMESPACE, JAEGER_DEPLOYMENT))


def names(rows, key):
    if not isinstance(rows, list) or not all(isinstance(row, dict) and isinstance(row.get(key), str) and row[key]
                                             for row in rows):
        raise Refused("The engine returned an unexpected listing")
    return sorted(row[key] for row in rows)


def inventory():
    """Every index, split into the daily Jaeger trace indices and all the others."""
    found = names(opensearch("GET", "_cat/indices?format=json&h=index&expand_wildcards=all"), "index")
    if len(set(found)) != len(found):
        raise Refused("The engine listed an index twice")
    targets = [name for name in found if TRACE_INDEX.fullmatch(name)]
    return targets, [name for name in found if name not in targets]


def templates():
    return names(opensearch("GET", "_cat/templates/jaeger*?format=json&h=name"), "name")


def delete_trace_indices(log=print, attempted=None):
    """Delete each daily Jaeger trace index by its exact name. Return (deleted, problems).

    Each name is added to `attempted` before its request is sent, so a caller can
    tell a refusal that changed nothing from a failure after a delete may have run.
    """
    attempted = [] if attempted is None else attempted
    targets, kept = inventory()
    before = templates()
    # The path of a delete is only ever a name that matched in full: no pattern, list or alias.
    if not all(TRACE_INDEX.fullmatch(name) for name in targets):
        raise Refused("The selection holds a name that is not a daily Jaeger trace index")
    log("Keeping %d other index(es): %s" % (len(kept), ", ".join(kept) or "none"))
    log("Deleting %d Jaeger trace index(es)" % len(targets))
    for name in targets:
        attempted.append(name)
        if opensearch("DELETE", name).get("acknowledged") is not True:
            raise RuntimeError("The engine did not acknowledge the deletion of " + name)
        log("Deleted " + name)
    problems = []
    try:
        # Ingestion continues, so today's indices may already exist again. Nothing else may have gone.
        gone = sorted(set(kept) - set(sum(inventory(), [])))
        if gone:
            problems.append("Indices outside the reset are gone, deleted by something else: " + ", ".join(gone))
        if templates() != before:
            problems.append("The Jaeger index templates changed during the reset")
    except Refused as error:
        problems.append("The state after the deletions could not be read: %s" % error)
    return targets, problems


def restart_jaeger(seconds=600):
    """Jaeger writes its index templates at startup and caches the service names it has recorded."""
    workload = "deployment/" + JAEGER_DEPLOYMENT
    run(["kubectl", "-n", JAEGER_NAMESPACE, "rollout", "restart", workload])
    # kubectl gives up first and says why; the process limit only covers a kubectl that hangs.
    run(["kubectl", "-n", JAEGER_NAMESPACE, "rollout", "status", workload, "--timeout=%ds" % seconds],
        limit=seconds + 60)


def restart_after_partial_reset(log=print):
    """Restart Jaeger after a reset that failed once a delete had been sent.

    Jaeger skips writing a service or operation it has cached for up to 12 hours,
    so with some trace indices gone the UI would miss services until then. A failed
    restart is reported and does not replace the error that stopped the reset.
    """
    try:
        restart_jaeger()
        log("Restarted %s/%s after the failed reset" % (JAEGER_NAMESPACE, JAEGER_DEPLOYMENT), file=sys.stderr)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        log("Jaeger could not be restarted after the failed reset; restart %s/%s by hand: %s"
            % (JAEGER_NAMESPACE, JAEGER_DEPLOYMENT, error), file=sys.stderr)


def has_service(value, service=CHECK_SERVICE):
    if isinstance(value, dict):
        return any(has_service(child, service) for child in value.values())
    if isinstance(value, list):
        return any(has_service(child, service) for child in value)
    return value == service


def verify_ingestion(popen=subprocess.Popen, attempts=30, pause=2):
    """Write and read a fresh trace, then find its service in the rebuilt service index."""
    collector, query = "http://127.0.0.1:14318", "http://127.0.0.1:16687"
    with ExitStack() as stack:
        for service, ports in (("jaeger-collector", "14318:4318"), ("jaeger-query", "16687:16686")):
            process = stack.enter_context(popen(
                ["kubectl", "-n", JAEGER_NAMESPACE, "port-forward", "svc/" + service, ports],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            stack.callback(process.terminate)
        verify_trace.check_trace(collector, query)
        for _ in range(attempts):
            try:
                if has_service(verify_trace.request(query + "/api/v3/services")):
                    print("Jaeger lists the service of the fresh trace")
                    return
            except (urllib.error.URLError, TimeoutError):
                pass
            time.sleep(pause)
    raise RuntimeError("Jaeger does not list the service of the fresh trace")


def summarize(env, lines):
    if env.get("GITHUB_STEP_SUMMARY"):
        with open(env["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write("## OpenSearch trace reset\n\n" + "\n".join("- " + line for line in lines) + "\n")


def main(argv=None, env=None):
    env = dict(os.environ) if env is None else env
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("operation", choices=("list", "reset"))
    parser.add_argument("--acknowledge", default="", help="For reset: the exact acknowledgment phrase")
    parser.add_argument("--validate-only", action="store_true", help="Check the request and stop before any cluster call")
    args = parser.parse_args(argv)
    try:
        validate_request(args.operation, args.acknowledge, env)
        if args.validate_only:
            print("The %s request is accepted" % args.operation)
            return 0
        check_target()
        if args.operation == "list":
            targets, kept = inventory()
            print("A reset would delete %d Jaeger trace index(es):" % len(targets))
            print("\n".join(targets) or "none")
            print("It would keep %d other index(es): %s" % (len(kept), ", ".join(kept) or "none"))
            summarize(env, ["Listed only; nothing was changed", "Jaeger trace indices: %d" % len(targets),
                            "Other indices: %d" % len(kept)])
            return 0
        attempted = []
        try:
            deleted, problems = delete_trace_indices(attempted=attempted)
        except Exception:
            if attempted:
                restart_after_partial_reset()
            raise
        restart_jaeger()
        verify_ingestion()
        summarize(env, ["Deleted Jaeger trace indices: %d" % len(deleted), "Restarted %s/%s" %
                        (JAEGER_NAMESPACE, JAEGER_DEPLOYMENT), "Fresh trace written and read"] + problems)
        if problems:
            raise RuntimeError("; ".join(problems))
        print("Reset complete: %d Jaeger trace index(es) deleted" % len(deleted))
        return 0
    except Refused as error:
        print("Refused, nothing was deleted: %s" % error, file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as error:
        print("Failed: %s\n%s" % (error, (error.stderr or "").strip()), file=sys.stderr)
        return 1
    except (RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        print("Failed: %s" % error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
