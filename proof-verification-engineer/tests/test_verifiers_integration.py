import json, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app.core.verification_engine import VerificationEngine
from app.storage.proof_packs_v3 import ProofPackStore
from app.verifiers.browser import BrowserVerifier
from app.verifiers.http import HTTPVerifier
from app.verifiers.models import PlannedCondition, VerificationContext, VerificationPlan, VerificationStep
from app.verifiers.registry import VerifierRegistry
from app.verifiers.shell import ShellVerifier

class Target(BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def do_GET(self):
        if self.path=="/ok": self.send_response(200); self.end_headers(); self.wfile.write(b"hello proof")
        elif self.path=="/bad": self.send_response(500); self.end_headers(); self.wfile.write(b"broken")
        elif self.path=="/json": self.send_response(200); self.end_headers(); self.wfile.write(json.dumps({"user":{"authenticated":True}}).encode())
        elif self.path=="/malformed": self.send_response(200); self.end_headers(); self.wfile.write(b"not json")
        elif self.path=="/redirect": self.send_response(302); self.send_header("Location","/ok"); self.end_headers()
        elif self.path=="/slow": time.sleep(.3); self.send_response(200); self.end_headers()
        elif self.path=="/page": self.send_response(200); self.end_headers(); self.wfile.write(b"<h1 id='title'>Dashboard</h1>")
        else: self.send_response(404); self.end_headers()

@pytest.fixture(scope="module")
def target():
    server=ThreadingHTTPServer(("127.0.0.1",0),Target); thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()

def http(target, op, expected=None, route="/ok", **params):
    return HTTPVerifier().verify("c",VerificationStep("http",op,expected,route,params),VerificationContext("http-run",target,timeout_seconds=.05))

def test_http_matrix(target):
    assert http(target,"reachable").status=="VERIFIED"
    assert http(target,"status",200).status=="VERIFIED" and http(target,"status",200,"/bad").status=="FAILED"
    assert http(target,"json_value",True,"/json",path="user.authenticated").status=="VERIFIED"
    assert http(target,"json_value",False,"/json",path="user.authenticated").status=="FAILED"
    assert http(target,"contains","proof").status=="VERIFIED" and http(target,"contains","nope").status=="FAILED"
    assert http(target,"redirect","/ok","/redirect").status=="VERIFIED"
    assert http(target,"json_value",True,"/malformed",path="user.authenticated").status=="INCONCLUSIVE"
    assert http(target,"status",200,"/slow").status=="INCONCLUSIVE"
    assert http("http://127.0.0.1:1","reachable").status in {"BLOCKED", "INCONCLUSIVE"}

def test_shell_and_browser_and_artifacts(tmp_path,target):
    workspace=tmp_path/"workspace"; workspace.mkdir(); (workspace/"test_sample.py").write_text("import sys\nprint('out')\nprint('err',file=sys.stderr)\ndef test_ok(): assert True\n")
    context=VerificationContext("browser-run",target,str(workspace),str(tmp_path/"artifacts"),2)
    shell=ShellVerifier().verify("shell",VerificationStep("shell","pytest",0,params={"args":("-q","-s")}),context)
    assert shell.status=="VERIFIED" and "out" in shell.metadata["stdout"] and "err" in shell.metadata["stderr"]
    browser=BrowserVerifier()
    assert browser.verify("b",VerificationStep("browser","navigate",target=target+"/page"),context).status=="VERIFIED"
    assert browser.verify("b",VerificationStep("browser","assert_visible",target="#title"),context).status=="VERIFIED"
    assert browser.verify("b",VerificationStep("browser","assert_text","Dashboard",target="#title"),context).status=="VERIFIED"
    failure=browser.verify("b",VerificationStep("browser","assert_visible",target="#missing"),context)
    browser.close(context.run_id)
    assert failure.status=="FAILED" and failure.artifacts and Path(failure.artifacts[0]).is_relative_to(tmp_path/"artifacts")

def test_proof_pack_reverification_preserves_frozen_contract(tmp_path):
    class Toggle:
        state="FAILED"
        def verify(self,c,s,x):
            from app.evidence.models import VerificationEvidence
            return VerificationEvidence(c,"http",s.operation,s.expected,self.state,self.state,x.run_id)
    toggle=Toggle(); condition=PlannedCondition("health","health",True,(VerificationStep("http","status",200),)); plan=VerificationPlan("contract_123",1,(condition,)); engine=VerificationEngine(VerifierRegistry({"http":toggle})); store=ProofPackStore(tmp_path)
    first=engine.verify("goal",plan,VerificationContext("run_1")); store.save(first); toggle.state="VERIFIED"; second=engine.verify("goal",plan,VerificationContext("run_2")); store.save(second)
    assert first.verdict=="FAILED" and second.verdict=="VERIFIED" and first.contract_id==second.contract_id and first.evidence[0].evidence_id!=second.evidence[0].evidence_id and store.load("run_1")["verdict"]=="FAILED"
