#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Render and compare the six real chart candidates locally without cluster credentials."""

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import scheduled_adapter as a


def fixture_images():
    sources = {}
    for key, repository, letter in (("jaeger", "jaegertracing/jaeger-snapshot", "a"),
                                    ("hotrod", "jaegertracing/example-hotrod-snapshot", "b")):
        tag, digest = letter * 40, "sha256:" + letter * 64
        sources[key] = {"repository": repository, "sourceCommit": tag, "tag": tag, "digest": digest,
                        "platformManifest": digest, "reference": f"{repository}:{tag}@{digest}"}
    return sources


def exercise(charts, jaeger_checkout):
    inventory = json.loads((a.ROOT / "docs/all-five-chart-access.json").read_bytes())
    expected_git = re.search(r'^JAEGER_CHART_COMMIT="([0-9a-f]{40})"$',
                             (a.ROOT / "scripts/demo/jaeger-chart.sh").read_text(), re.M)[1]
    got = subprocess.check_output(["git", "-C", jaeger_checkout, "rev-parse", "HEAD"], text=True).strip()
    a.need(got == expected_git, "Fixture chart Git commit differs from the reviewed source")
    images = fixture_images()
    receipt = {"helm": "v4.3.2", "clusterAccess": False, "jaegerChartCommit": got,
               "scope": "real chart packages; synthetic image provenance and Grafana password; client-side upgrade renders",
               "releases": {}, "supplementalObjects": 0}
    with tempfile.TemporaryDirectory(prefix="six-chart-candidates-") as scratch:
        temp = Path(scratch)
        env = {"PATH": os.environ["PATH"], "KUBECONFIG": os.devnull, "HOME": scratch}
        a.need(a.command(["helm", "version", "--template={{.Version}}"], env=env) == b"v4.3.2", "Use Helm 4.3.2")
        a.command(["helm", "package", jaeger_checkout / "charts/jaeger", "--destination", temp], env=env)
        local, = temp.glob("jaeger-*.tgz")
        for key, (ns, name, _) in a.RELEASES.items():
            pkg = local
            if not key.endswith("-jaeger"):
                candidates = [f for f in inventory["chartArchiveSHA256"] if re.fullmatch(re.escape(a.CHARTS[key]) + r"-[0-9.]+\.tgz", f)]
                a.need(len(candidates) == 1, "Missing reviewed package")
                filename = candidates[0]
                pkg = charts / filename
                a.need(a.sha(pkg.read_bytes()) == inventory["chartArchiveSHA256"][filename], "Fixture archive digest differs")
            inputs = a.values_for(key, a.ROOT, images)
            if key == "oci-monitoring":
                # Simulate the deterministic value fetched by Helm's live
                # Grafana Secret lookup without contacting a cluster.
                a.override(inputs, ("grafana", "adminPassword"), "synthetic-local-fixture")
            def render(data):
                raw = a.command(["helm", "template", name, pkg, "-n", ns, "--kube-version=1.36.1",
                                 "--is-upgrade", "--values", "-"], body=a.encode(data), env=env)
                return a.docs(raw)
            candidate = render(inputs)
            previous = json.loads(a.encode(inputs))
            if key.endswith("-jaeger"):
                for prefix in ("allInOne", "hotrod"):
                    previous[prefix]["image"]["tag"] = "c" * 40
            prior = render(previous)
            a.validate_change(key, prior, candidate, images)
            supplements = a.supplemental_for(key, a.ROOT)
            covered = a.inventory(key, candidate, supplements)
            before, after = a.index(prior, ns), a.index(candidate, ns)
            receipt["releases"][key] = {"chart": a.CHARTS[key], "helmObjects": len(after), "trackedLiveObjects": len(covered),
                                       "supplementalObjects": len(supplements), "changedObjects": sorted(k for k in before if before[k] != after[k]),
                                       "snapshotPlatform": "linux/amd64"}
            receipt["supplementalObjects"] += len(supplements)
        a.need(receipt["supplementalObjects"] == 12, "Fixture supplemental inventory differs")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--charts", type=Path, required=True, help="Directory with the four reviewed chart archives")
    parser.add_argument("--jaeger-checkout", type=Path, required=True, help="Pinned chart checkout with packaged dependencies")
    opts = parser.parse_args()
    print(json.dumps(exercise(opts.charts, opts.jaeger_checkout), indent=2))
