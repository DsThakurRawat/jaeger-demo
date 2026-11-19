# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Privately stage and authenticate the native admission parameters for one daily slot."""

from datetime import datetime, timezone
from http.client import HTTPSConnection, HTTPException
import json
from pathlib import Path
import re
import ssl

import scheduled_approval_types as t

STORE = Path("/var/lib/jaeger-bundles")
KEY = Path("/var/run/jaeger-bundle-keys/verification-key.pem")
TYPE = t.RESOURCE + "." + t.GROUP
CSA_HELPER = "/usr/local/bin/jaeger-helm-preview"
SERVER = ("--dry-run=server", "--field-manager=helm", "-o", "json")
# Independently preserve container privileges, injected credentials, host access
# and deployment identity. Changing these belongs to operator establishment.
POD_SETTINGS = {
    "serviceAccount",
    "serviceAccountName",
    "automountServiceAccountToken",
    "volumes",
    "hostNetwork",
    "hostPID",
    "hostIPC",
    "shareProcessNamespace",
    "hostUsers",
    "securityContext",
    "imagePullSecrets",
    "runtimeClassName",
    "nodeName",
    "priorityClassName",
    "nodeSelector",
    "schedulerName",
    "tolerations",
    "affinity",
    "resourceClaims",
}
CONTAINER_SETTINGS = {"securityContext", "volumeMounts", "volumeDevices", "envFrom"}


def require(ok, message):
    if not ok:
        raise ValueError(message)


def client():
    # Lazy import keeps the Kubernetes installation/verification tool credential-free.
    import scheduled_adapter

    return scheduled_adapter


def resource_id(obj, ns):
    kind = obj["kind"]
    group = obj["apiVersion"].split("/")[0] if "/" in obj["apiVersion"] else ""
    target = {"Ingress": "ingresses", "Prometheus": "prometheuses"}.get(kind, kind.lower() + "s")
    return obj["metadata"].get("namespace", ns), group, target, obj["metadata"]["name"]


def podspec(obj):
    spec = obj["spec"]
    if obj["kind"] == "CronJob":
        spec = spec["jobTemplate"]["spec"]
    return spec["template"]["spec"]


def image_repo(value):
    name = value.split("@")[0]
    path, _, last = name.rpartition("/")
    return (path + "/" if path else "") + last.split(":")[0]


def retained_image_settings(old, new):
    require(len(old) == len(new), "Privileged collector containers require operator establishment")
    for left, right in zip(old, new):
        require(
            {k: v for k, v in left.items() if k != "image"}
            == {k: v for k, v in right.items() if k != "image"},
            "Privileged collector settings require operator establishment",
        )
        if left["image"] != right["image"]:
            require(
                image_repo(left["image"]) == image_repo(right["image"])
                and re.search(r"@sha256:[0-9a-f]{64}$", right["image"]),
                "Privileged collector image changes require a same-repository digest",
            )


def safe_transition(live, target):
    meta, new_meta = live["metadata"], target["metadata"]
    require(
        live["kind"] == target["kind"]
        and live["apiVersion"] == target["apiVersion"]
        and all(
            meta.get(k) == new_meta.get(k)
            for k in (
                "name",
                "namespace",
                "uid",
                "ownerReferences",
                "finalizers",
                "deletionTimestamp",
                "creationTimestamp",
            )
        ),
        "Approval would change workload ownership",
    )
    if live["kind"] == "Service":
        fields = (
            "clusterIP",
            "clusterIPs",
            "ipFamilies",
            "ipFamilyPolicy",
            "selector",
            "type",
            "externalName",
            "loadBalancerIP",
            "loadBalancerClass",
            "loadBalancerSourceRanges",
            "externalIPs",
        )
        require(
            all(live["spec"].get(k) == target["spec"].get(k) for k in fields),
            "Service binding change requires an operator",
        )
    if live["kind"] not in {"Deployment", "DaemonSet", "StatefulSet", "CronJob"}:
        return
    a, b = podspec(live), podspec(target)
    require(
        all(a.get(k) == b.get(k) for k in POD_SETTINGS),
        "Pod access or scheduling change requires an operator",
    )
    for name in ("containers", "initContainers", "ephemeralContainers"):
        left, right = a.get(name, []), b.get(name, [])
        require(
            [c["name"] for c in left] == [c["name"] for c in right],
            "Container inventory change requires an operator",
        )
        for prior, desired in zip(left, right):
            require(
                all(prior.get(k) == desired.get(k) for k in CONTAINER_SETTINGS),
                "Container privilege change requires an operator",
            )

            def refs(c):
                return [(e["name"], e["valueFrom"]) for e in c.get("env", []) if "valueFrom" in e]

            require(
                refs(prior) == refs(desired), "Credential or injected environment change requires an operator"
            )
        if live["kind"] == "DaemonSet" and meta["name"] == "otel-collector-agent":
            retained_image_settings(left, right)
    if live["kind"] == "DaemonSet" and meta["name"] == "otel-collector-agent":
        omit = {"containers", "initContainers", "ephemeralContainers"}
        require(
            {k: v for k, v in a.items() if k not in omit} == {k: v for k, v in b.items() if k not in omit},
            "Privileged collector pod changes require an operator",
        )
    if live["kind"] == "StatefulSet" and meta.get("namespace") == "opensearch":
        fields = (
            "replicas",
            "volumeClaimTemplates",
            "persistentVolumeClaimRetentionPolicy",
            "selector",
            "serviceName",
        )
        require(
            all(live["spec"].get(k) == target["spec"].get(k) for k in fields),
            "OpenSearch storage change requires an operator",
        )


def get(kind, name, ns):
    return json.loads(client().kube("-n", ns, "get", kind, name, "-o", "json"))


def dry_patch(ident, kind, patch):
    ns, g, r, n = ident
    return json.loads(
        client().kube(
            "-n",
            ns,
            "patch",
            r + ("." + g if g else ""),
            n,
            "--type=" + kind,
            "--patch-file=/dev/stdin",
            *SERVER,
            body=t.canonical(patch),
        )
    )


def apply_patch(
    ident,
    version,
    target,
    *,
    authority="kubernetes.default.svc",
    account=Path("/var/run/secrets/kubernetes.io/serviceaccount"),
):
    # Exact ApplyPatch (without kubectl apply's client-side ownership migration).
    # HTTPConnection never follows redirects. The API endpoint, CA and SA projection
    # are fixed in the installed process and cannot be specified by a bundle.
    ns, group, resource, name = ident
    require(
        all(re.fullmatch(r"[a-z0-9][a-z0-9.-]*", v) for v in (ns, resource, name))
        and version == (group + "/v1" if group else "v1"),
        "Unexpected server-side preview target",
    )
    base = "/apis/" + version if group else "/api/v1"
    path = (
        base
        + "/namespaces/"
        + ns
        + "/"
        + resource
        + "/"
        + name
        + "?dryRun=All&fieldManager=helm&force=false&fieldValidation=Strict"
    )
    conn = None
    try:
        conn = HTTPSConnection(
            authority, context=ssl.create_default_context(cafile=str(account / "ca.crt")), timeout=45
        )
        token = (account / "token").read_text().strip()
        conn.request(
            "PATCH",
            path,
            body=t.canonical(target),
            headers={
                "Content-Type": "application/apply-patch+yaml",
                "Accept": "application/json",
                "Authorization": "Bearer " + token,
            },
        )
        result = conn.getresponse()
        require(result.status == 200, "Existing workload server-side preview was rejected")
        raw = result.read(8 * 1024 * 1024 + 1)
        require(len(raw) <= 8 * 1024 * 1024, "Workload server-side preview is too large")
        return json.loads(raw)
    except (HTTPException, OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Private Kubernetes server-side preview failed") from None
    finally:
        if conn is not None:
            conn.close()


def preview(original, desired, live, ns, release, method):
    """Reproduce the existing Helm mode. Server defaulting/admission must succeed."""
    ident = resource_id(live, ns)
    # Helm Build defaults the namespace on both inputs, then visits only target to add ownership.
    prior = json.loads(t.canonical(original))
    target = json.loads(t.canonical(desired))
    for obj in (prior, target):
        obj["metadata"].setdefault("namespace", ident[0])
    m = target["metadata"]
    m.setdefault("labels", {})["app.kubernetes.io/managed-by"] = "Helm"
    if not m.get("annotations"):
        m["annotations"] = {}
    m["annotations"].update({"meta.helm.sh/release-name": release, "meta.helm.sh/release-namespace": ns})
    if method == "ssa":
        return apply_patch(ident, target["apiVersion"], target)
    require(method == "csa", "Unrecognized stored Helm apply method")
    result = json.loads(
        client().command([CSA_HELPER], body=t.canonical({"original": prior, "desired": target, "live": live}))
    )
    require(
        set(result) == {"type", "patch"}
        and result["type"] in {"strategic", "merge"}
        and isinstance(result["patch"], dict),
        "Invalid offline Helm patch preview",
    )
    return dry_patch(ident, result["type"], result["patch"]) if result["patch"] else live


def prepare_variants(files, slot, receipt):
    adapter = client()
    plan = adapter.validate_plan(files, slot, receipt["configurationSHA"])
    values = {part: {} for part in t.PARTITIONS}
    allowed = t.allowed()
    for key, row in plan["releases"].items():
        if adapter.component(key) not in plan["selected"]:
            continue
        ns, release, _ = adapter.RELEASES[key]
        records, latest, rollback = adapter.history(key)
        require(
            records == row["records"] and adapter.apply_method(latest) == row["applyMethod"],
            "History changed before admission preview",
        )
        # Helm chooses last successful release for the original upgrade manifest on recovery.
        success = adapter.index(adapter.release_docs(rollback), ns)
        original = (
            success
            if rollback["info"]["status"] == "deployed"
            else adapter.index(adapter.release_docs(latest), ns)
        )
        require(
            t.digest(success) == row["rollbackRenderSHA256"],
            "Stored recovery manifest differs from frozen rollback",
        )
        _, _, candidate, extras = adapter.material(files, key)
        adapter.validate_change(key, adapter.release_docs(latest), candidate, plan["images"])
        adapter.validate_change(key, adapter.release_docs(rollback), candidate, plan["images"])
        targets = adapter.inventory(key, candidate, extras)
        supplements = set(adapter.index(extras, ns))
        for ident, obj in targets.items():
            rid = resource_id(obj, ns)
            extra = ident in supplements
            if rid not in allowed:
                require(not extra and original[ident] == obj, "Change outside the ordinary preview inventory")
                continue
            # Semantically identical chart inputs use the installed no-op guard.
            if not extra and original[ident] == obj:
                continue
            live = adapter.get_object(ident)
            require(
                adapter.projection(live) == row["states"][ident],
                "Workload drifted before independent approval",
            )
            if extra:
                operations = adapter.supplemental_changes(obj, live)
                if not operations:
                    continue
                after = dry_patch(rid, "json", adapter.tests(live) + operations)
            else:
                after = preview(original[ident], obj, live, ns, release, row["applyMethod"])
            if t.variant(live) == t.variant(after):
                continue
            safe_transition(live, after)
            namespace, group, resource, name = rid
            handle = (group or "core") + "/" + resource + "/" + name
            part = t.partition(namespace, handle)
            require(handle not in values[part], "Duplicate independent approval target")
            # For the existent deployed baseline, the original admitted object is the
            # rollback expectation. Any different recovered output needs operator review.
            values[part][handle] = {
                "uid": live["metadata"]["uid"],
                "variants": [t.variant(live), t.variant(after)],
            }
        require(
            adapter.history(key)[0] == records, "Helm records drifted during the server admission preview"
        )
    return values


def aggregate(slot, receipt, uid):
    all_parts = {}
    for bucket in t.PARTITIONS:
        scope = t.namespace(bucket)
        obj = get(TYPE, t.name(bucket), t.OPS)
        part = obj["spec"]
        require(
            set(part)
            == {"scope", "partition", "slot", "requestUID", "configurationSHA", "bundleSHA256", "changes"}
            and (part["scope"], part["partition"], part["slot"], part["requestUID"])
            == (scope, bucket, slot, uid)
            and all(t.partition(scope, handle) == bucket for handle in part["changes"])
            and all(part[k] == v for k, v in receipt.items())
            and len(t.canonical(part)) <= t.MAX_PART,
            "Missing, oversized or incorrect daily approval parameter",
        )
        all_parts[bucket] = {"uid": obj["metadata"]["uid"], "spec": part}
    return t.digest(all_parts)


def publish_approvals(files, slot, receipt):
    t.validate_receipt(slot, receipt)
    request = get("configmap", "daily-hosted-update", t.OPS)
    uid = request["metadata"]["uid"]
    data = request["data"]
    # A crash can leave unadvertised partial projections. Restage them only before
    # the new slot is made visible. Expiry uses trusted pod time, never caller time.
    if data["state"] != "empty":
        require(
            data["state"] in {"succeeded", "recovered", "ready", "requested"}
            and slot > data["slot"]
            and (
                data["state"] in {"succeeded", "recovered"}
                or datetime.now(timezone.utc)
                >= datetime.fromisoformat(data["expiresAt"].replace("Z", "+00:00"))
            ),
            "Advertised or active slot forbids preparation",
        )
    changes = prepare_variants(files, slot, receipt)
    desired = {}
    for bucket in t.PARTITIONS:
        scope = t.namespace(bucket)
        spec = dict(
            scope=scope, partition=bucket, slot=slot, requestUID=uid, **receipt, changes=changes[bucket]
        )
        require(
            len(t.canonical(spec)) <= t.MAX_PART
            and len(changes[bucket]) <= t.MAX_CHANGES
            and all(t.partition(scope, handle) == bucket for handle in changes[bucket]),
            "Daily approval exceeds private resource limit",
        )
        desired[bucket] = spec
    staged = {}
    for bucket, spec in desired.items():
        previous = get(TYPE, t.name(bucket), t.OPS)
        require(
            previous["spec"]["scope"] == t.namespace(bucket) and previous["spec"]["partition"] == bucket,
            "Misdirected daily approval partition",
        )
        patched = previous
        if previous["spec"] != spec:
            operations = client().tests(previous) + [{"op": "replace", "path": "/spec", "value": spec}]
            patched = json.loads(
                client().kube(
                    "-n",
                    t.OPS,
                    "patch",
                    TYPE,
                    t.name(bucket),
                    "--type=json",
                    "--patch-file=/dev/stdin",
                    "-o",
                    "json",
                    body=t.canonical(operations),
                )
            )
        require(
            patched["spec"] == spec and patched["metadata"]["uid"] == previous["metadata"]["uid"],
            "Native admission did not preserve the staged projection",
        )
        staged[bucket] = {"uid": patched["metadata"]["uid"], "spec": spec}
    calculated = t.digest(staged)
    require(aggregate(slot, receipt, uid) == calculated, "Incomplete private staging readback")
    return calculated


def verify_approvals(slot, receipt):
    import scheduled_bundle

    t.validate_receipt(slot, receipt)
    expected = scheduled_bundle.approval_commitment(STORE, slot, receipt, verification_key=KEY)
    uid = get("configmap", "daily-hosted-update", t.OPS)["metadata"]["uid"]
    require(aggregate(slot, receipt, uid) == expected, "Daily parameters differ from their sealed aggregate")
    return True
