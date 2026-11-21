#!/usr/bin/env python3
# Copyright (c) 2026 The Jaeger Authors.
# SPDX-License-Identifier: Apache-2.0
"""Exercise the native workload guards only on a fresh loopback Kubernetes API server."""

import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import permissions as p


def exercise(cfg, context):
    admin=["kubectl","--kubeconfig",str(cfg),"--context",context]
    def call(client,*args,doc=None,ok=True):
        r=subprocess.run(client+list(args),input=json.dumps(doc).encode() if doc is not None else None,capture_output=True,timeout=90)
        if ok and r.returncode:
            raise AssertionError(r.stderr.decode()[:1800])
        return r
    view=json.loads(call(admin,"config","view","--minify","-o","json").stdout)
    if urlsplit(view["clusters"][0]["cluster"]["server"]).hostname!="127.0.0.1":
        raise ValueError("Fresh loopback API server required")
    def create(o):
        if o["kind"] == "List":
            call(admin,"create","-f","-",doc=o)
            return
        return json.loads(call(admin,"create","-f","-","-o","json",doc=o).stdout)
    create(p.resource("v1","Namespace",p.OPS))
    for ns in ("jaeger","opensearch","otel-demo"):
        create(p.resource("v1","Namespace",ns))
    for a in (p.PREPARER,p.EXECUTOR):
        create(p.resource("v1","ServiceAccount",a,p.OPS))
    slot="2026-11-19T13:00:00Z"
    source={"slot":slot,"configurationSHA":"b"*40,"bundleSHA256":"c"*64,"state":"requested","engine":"scripts-v1"}
    record=create(p.resource("v1","ConfigMap","daily-hosted-update",p.OPS,data=source))
    owner="daily/"+slot+"/"+record["metadata"]["uid"]
    cp=p.resource("v1","ConfigMap",p.CHECKPOINT,"default",data={})
    cp["metadata"]["annotations"]={p.PROTOCOL:"v1"}
    create(cp)
    def dep(name,ns):
        return create(p.resource("apps/v1","Deployment",name,ns,spec={"selector":{"matchLabels":{"app":name}},
                    "template":{"metadata":{"labels":{"app":name}},"spec":{"containers":[{"name":name,"image":"registry.invalid/fixture:v1"}]}}}))
    objects = {(ns,name):dep(name,ns) for ns in ("default","jaeger") for name in ("jaeger","jaeger-hotrod","trace-generator")}
    app=dep("frontend","otel-demo")
    create(p.resource("v1","ConfigMap","user-config","default",data={"value":"stable"}))
    create(p.resource(p.RBAC,"ClusterRole","otel-collector",rules=[]))
    create(p.resource("v1","Secret","prometheus-grafana","default",type="Opaque",data={"fixture":"eA=="}))
    old=app
    import copy
    candidate=copy.deepcopy(app)
    candidate["spec"]["template"]["spec"]["containers"][0]["image"]="registry.invalid/fixture:v2"
    review={k:source[k] for k in ("slot","configurationSHA","bundleSHA256")}
    review["changes"]=[{"namespace":"otel-demo","group":"apps","resource":"deployments","name":"frontend","states":[old,candidate]}]
    guards,roles=p.regular(review)
    special,special_roles=p.workload(review)
    # Install guards before role grants and verify all policies are admitted by the API.
    create({"apiVersion":"v1","kind":"List","items":guards+special+p.history_guard()})
    request_role=p.bindings("fixture-existing-request-actor",p.OPS,[p.rule("","configmaps",["get","patch"],["daily-hosted-update"])],(p.EXECUTOR,))
    create({"apiVersion":"v1","kind":"List","items":list(p.read_access())+roles+special_roles+list(p.history_grants())+request_role})
    with tempfile.TemporaryDirectory() as temp:
        def client(name):
            token=call(admin,"-n",p.OPS,"create","token",name,"--duration=10m").stdout.decode().strip()
            doc=json.loads(call(admin,"config","view","--raw","--minify","-o","json").stdout)
            doc["users"]=[{"name":"fixture-role","user":{"token":token}}]
            doc["contexts"][0]["context"]["user"]="fixture-role"
            file=Path(temp)/name
            file.write_text(json.dumps(doc))
            file.chmod(0o600)
            return ["kubectl","--kubeconfig",str(file),"--context",context]
        actor,planner=client(p.EXECUTOR),client(p.PREPARER)
        def patch(client,ns,kind,name,obj,accept,reason=""):
            r=call(client,"-n",ns,"patch",kind,name,"--type=merge","--patch",json.dumps(obj),ok=False)
            if bool(r.returncode)==accept or reason and reason not in r.stderr.decode():
                raise AssertionError(f"Unexpected {kind}/{name} API result {r.returncode}: {r.stderr.decode()[:500]}")
        lock={"metadata":{"annotations":{p.HOLDER:owner}}}
        time.sleep(1)
        patch(planner,"default","deployment","jaeger",{"spec":{"replicas":3}},False,"forbidden")
        patch(actor,p.OPS,"configmap","daily-hosted-update",{"data":{"state":"running"}},False)
        patch(actor,"default","configmap",p.CHECKPOINT,{"metadata":{"annotations":{p.HOLDER:"foreign"}}},False,"matching daily")
        patch(actor,"default","configmap",p.CHECKPOINT,lock,True)
        patch(actor,p.OPS,"configmap","daily-hosted-update",{"data":{"state":"running"}},True)
        patch(admin,"default","configmap",p.CHECKPOINT,{"metadata":{"annotations":{p.HOLDER:"foreign"}}},False,"exact holder")
        for ns in ("default","jaeger"):
            for name,repo in (("jaeger","jaeger-snapshot"),("jaeger-hotrod","example-hotrod-snapshot")):
                spec=objects[ns,name]["spec"]["template"]["spec"]
                spec["containers"][0]["image"]="docker.io/jaegertracing/"+repo+":"+"c"*40+"@sha256:"+"d"*64
                patch(actor,ns,"deployment",name,{"spec":{"template":{"spec":spec}}},True)
                spec["containers"][0]["image"]="registry.invalid/unreviewed:v2"
                patch(actor,ns,"deployment",name,{"spec":{"template":{"spec":spec}}},False,"Unreviewed change")
            def restart(value):
                return {"spec":{"template":{"metadata":{"annotations":{"kubectl.kubernetes.io/restartedAt":value}}}}}
            patch(actor,ns,"deployment","trace-generator",restart(slot),True)
            patch(actor,ns,"deployment","trace-generator",restart("2020-01-01T00:00:00Z"),False,"Unreviewed change")
        patch(actor,"default","configmap","user-config",{"data":{"value":"stable"}},True)
        patch(actor,"default","configmap","user-config",{"data":{"value":"unexpected"}},False,"Unreviewed change")
        patch(actor,"default","secret","prometheus-grafana",{"data":{"fixture":"eA=="}},True)
        patch(actor,"default","secret","prometheus-grafana",{"data":{"fixture":"eQ=="}},False)
        patch(actor,"default","clusterrole","otel-collector",{},True)
        patch(actor,"default","clusterrole","otel-collector",{"metadata":{"labels":{"unreviewed":"yes"}}},False,"Unreviewed change")
        patch(actor,"otel-demo","deployment","frontend",{"spec":candidate["spec"]},True)
        patch(actor,"otel-demo","deployment","frontend",{"spec":{"replicas":5}},False,"independently approved")
        patch(actor,"default","configmap",p.CHECKPOINT,{"data":{"oci-jaeger":"f"*64}},True)
        patch(actor,"default","configmap",p.CHECKPOINT,{"data":{"arbitrary":"f"*64}},False,"matching daily")
        secret=p.resource("v1","Secret","sh.helm.release.v1.jaeger.v2","default",type="helm.sh/release.v1",data={"release":base64.b64encode(b"synthetic-envelope").decode()})
        secret["metadata"]["labels"]={"owner":"helm","version":"2","name":"jaeger","status":"pending-upgrade"}
        call(actor,"create","-f","-",doc=secret)
        denied=p.resource("v1","Secret","unrelated","default",type="Opaque",data={"x":"eA=="})
        result=call(actor,"create","--dry-run=server","-f","-",doc=denied,ok=False)
        assert result.returncode and b"canonical hosted" in result.stderr
        patch(admin,p.OPS,"configmap","daily-hosted-update",{"data":{"state":"verified"}},True)
        patch(actor,"default","configmap",p.CHECKPOINT,{"metadata":{"annotations":{p.HOLDER:None}}},True)
        patch(actor,"default","configmap","user-config",{"data":{"value":"stable"}},False)
    print(json.dumps({"nativeAdmission":True,"twoServiceAccountTokens":True,"sixSnapshotAndRestartWorkloads":True,
                      "approvedOrdinaryChange":True,"historyEnvelope":True,"componentAndLockTransitions":True}))


if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--kubeconfig",type=Path,required=True)
    ap.add_argument("--context",choices=["fixture"],required=True)
    a=ap.parse_args()
    if not __debug__:
        ap.error("Enable fixture assertions")
    exercise(a.kubeconfig,a.context)
