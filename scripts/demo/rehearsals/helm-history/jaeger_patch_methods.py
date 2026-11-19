# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0

"""Measure real chart upgrade API writes under Helm's inherited apply method."""

import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import scheduled_adapter as a
from all_five_candidates import fixture_images


GROUP = "rbac.authorization.k8s.io"
PLURALS = {"ServiceAccount":"serviceaccounts", "ConfigMap":"configmaps", "Service":"services",
           "Deployment":"deployments", "CronJob":"cronjobs"}


def stored_objects(kube, namespace, candidate):
    """Compare all stored content, including status and ownership metadata."""
    result = {}
    for obj in candidate:
        resource = PLURALS[obj["kind"]]
        name = obj["metadata"]["name"]
        live = json.loads(kube(["get", resource, name, "-n", namespace, "-o", "json"]).stdout)
        for field in ("managedFields", "resourceVersion", "generation"):
            live["metadata"].pop(field, None)
        result[f"{resource}/{name}"] = live
    return result


def delta_paths(left, right, path=""):
    if type(left) is type(right) is dict:
        keys = left.keys() | right.keys()
        return sorted({p for k in keys for p in (
            [path + "/" + k] if k not in left or k not in right
            else delta_paths(left[k], right[k], path + "/" + k))})
    if type(left) is type(right) is list and len(left) == len(right):
        return [p for i, (a, b) in enumerate(zip(left, right))
                for p in delta_paths(a, b, path + "/" + str(i))]
    return [] if type(left) is type(right) and left == right else [path]


def exercise(root, cfg, kube, apply, chart_checkout):
    env = dict(os.environ, KUBECONFIG=str(cfg), HELM_PLUGINS=str(a.ROOT / "scripts/demo/helm-plugins"))
    a.command(["helm", "package", chart_checkout / "charts/jaeger", "--destination", root], env=env)
    packages = [p for p in root.glob("jaeger-*.tgz") if p.name != "jaeger-0.1.0.tgz"]
    assert len(packages) == 1
    chart = packages[0]
    audit = root / "audit.log"
    out = {}
    for stack in ("oci", "otel"):
        key = stack + "-jaeger"
        for method in ("ssa", "csa"):
            ns = f"fixture-{stack}-{method}"
            user = "writer-" + ns
            kube(["create", "namespace", ns])
            values = a.values_for(key, a.ROOT, fixture_images())
            baseline = json.loads(a.encode(values))
            for field in ("allInOne", "hotrod"):
                baseline[field]["image"]["tag"] = "f" * 40
            a.command(["helm", "install", "jaeger", chart, "-n", ns, "-f", "-", "--wait=hookOnly",
                       "--server-side=" + str(method == "ssa").lower()], body=a.encode(baseline), env=env)
            as_user = ["--kube-as-user", user, "--kube-as-group", "system:authenticated"]
            meta = {"namespace": ns, "name": user}
            def set_role(extra):
                rules = [{"apiGroups":["*"],"resources":["*"],"verbs":["get","list","watch"]},
                         {"apiGroups":[""],"resources":["secrets"],"verbs":["create","update","delete"]},
                         {"apiGroups":["apps"],"resources":["deployments"],"resourceNames":["jaeger","jaeger-hotrod"],"verbs":["patch"]}]
                apply({"apiVersion":GROUP+"/v1","kind":"Role","metadata":meta,"rules":rules + extra})
            set_role([])
            apply({"apiVersion":GROUP+"/v1","kind":"RoleBinding","metadata":meta,
                   "roleRef":{"apiGroup":GROUP,"kind":"Role","name":user},"subjects":[{"kind":"User","apiGroup":GROUP,"name":user}]})
            args = ["helm", "upgrade", "jaeger", chart, "-n", ns, "-f", "-", "--wait=hookOnly", "--reset-values",
                    "--install=false","--create-namespace=false","--skip-crds","--rollback-on-failure=false","--server-side=auto", "-o","json", *as_user]
            preview = json.loads(a.command([*args,"--dry-run=server"], body=a.encode(values), env=env))
            assert preview["apply_method"] == method
            candidate = a.release_docs(preview)
            original = a.release_docs(json.loads(a.command(["helm","status","jaeger","-n",ns,"-o","json"], env=env)))
            old, new = a.index(original, ns), a.index(candidate, ns)
            changed = {ident for ident in old if old[ident] != new[ident]}
            assert len(changed) == 2 and all(":Deployment:" in name for name in changed)
            expected = root / f"{ns}-reviewed.yaml"
            expected.write_bytes(b"\n---\n".join(a.encode(obj) for obj in candidate))
            env["JAEGER_APPROVED_MANIFEST"] = str(expected)
            def patch_preflight():
                def scoped_kube(*args, body=None):
                    return kube(["--as", user, "--as-group", "system:authenticated", *args],
                                body=body.decode()).stdout.encode()
                with patch.object(a, "kube", scoped_kube):
                    try:
                        a.check_jaeger_patch_access(ns, candidate)
                    except a.AdapterError:
                        return False
                return True
            assert not patch_preflight()
            def attempt():
                offset = audit.stat().st_size
                try:
                    a.command([*args,"--post-renderer","verify-frozen-manifests"], body=a.encode(values), env=env)
                    passed = True
                except a.AdapterError:
                    passed = False
                successful, forbidden = set(), set()
                with audit.open("rb") as stream:
                    stream.seek(offset)
                    for line in stream:
                        evt = json.loads(line)
                        ref = evt.get("objectRef", {})
                        if (evt.get("impersonatedUser", {}).get("username") != user or ref.get("namespace") != ns or
                            evt["verb"] != "patch" or "dryRun" in urlsplit(evt["requestURI"]).query):
                            continue
                        name = ref["resource"] + "/" + ref["name"]
                        code = evt["responseStatus"]["code"]
                        if code == 200:
                            successful.add(name)
                        elif code == 403:
                            forbidden.add(name)
                return passed, sorted(successful), sorted(forbidden)
            passed, allowed, denied = attempt()
            history = json.loads(a.command(["helm","history","jaeger","-n",ns,"-o","json"], env=env))
            row = {"releaseObjects":len(candidate), "changed":sorted(changed), "narrowSucceeded":passed,
                   "narrowPatches":allowed,"narrowForbidden":denied,"narrowStatus":history[-1]["status"]}
            # Capture the full named-grant behavior from a clean previous state.
            a.command(["helm","rollback","jaeger","1","-n",ns,"--wait=hookOnly"], env=env)
            extra = []
            for obj in candidate:
                api, kind, _, name = a.identity(obj, ns).split(":")
                group = api.split("/")[0] if "/" in api else ""
                extra.append({"apiGroups":[group],"resources":[PLURALS[kind]],"resourceNames":[name],"verbs":["patch"]})
            set_role(extra)
            assert patch_preflight()
            before = stored_objects(kube, ns, candidate)
            accepted, patches, failures = attempt()
            assert accepted and not failures
            after = stored_objects(kube, ns, candidate)
            row["storedChanges"] = {name: delta_paths(obj, after[name])
                                    for name, obj in before.items() if obj != after[name]}
            assert row["storedChanges"] == {name: ["/spec/template/spec/containers/0/image"]
                                            for name in ("deployments/jaeger", "deployments/jaeger-hotrod")}
            row["namedPatchPreflight"] = {"deploymentsOnly": False, "allChartObjects": True}
            row["namedPatches"] = patches
            row["namespacedAPIs"] = sorted({p.split("/")[0] for p in patches})
            print(ns, json.dumps(row), flush=True)
            if method == "ssa":
                assert not passed and denied and len(patches) == len(candidate)
            else:
                # CSA computes three-way patches, including managed/defaulted
                # metadata; measure its behavior rather than assume two PATCHes.
                assert {"deployments/jaeger", "deployments/jaeger-hotrod"} <= set(patches)
            assert bool(denied) != passed
            out[ns] = row
    return {"scope":"real OCI and OTel Jaeger charts; isolated namespaces; synthetic snapshot tags; no Pod controllers or health checks",
            "helmWait":"hookOnly for local API fixture", "applyMode":"auto inherits stored apply_method unchanged", "outcomes":out}
