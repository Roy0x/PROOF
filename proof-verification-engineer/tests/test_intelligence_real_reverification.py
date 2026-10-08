"""Offline controlled failure/repair with real v0.3 verifiers and fake model output."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from app.core.intelligence import TrustedContext
from app.core.intelligence_workflow import IntelligenceWorkflow
from app.core.verification_engine import VerificationEngine
from app.llm.token_factory import ModelResponse, ModelUsage, ReasoningPolicy
from app.storage.proof_packs_v3 import ProofPackStore
from app.verifiers.browser import BrowserVerifier
from app.verifiers.http import HTTPVerifier
from app.verifiers.registry import VerifierRegistry
from app.verifiers.shell import ShellVerifier


class ControlledApp(BaseHTTPRequestHandler):
    broken = True
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/":
            status, body = 200, b"Production online"
        elif self.path == "/login":
            status = 200
            body = b'''<h1>Sign in</h1><input type="password" id="password"><button id="submit">Sign in</button>
<script>document.querySelector('#submit').onclick = async () => {
const response = await fetch('/api/login', {method:'POST',headers:{'Content-Type':'application/json'},
body:JSON.stringify({password:document.querySelector('#password').value})});
if (response.ok) location.href='/dashboard'; };</script>'''
        elif self.path == "/dashboard":
            status, body = (200, b"<h1 id='dashboard-heading'>Deployment dashboard</h1>") if self.server.authenticated else (401, b"Auth required")
        else:
            status, body = 404, b"Not found"
        self.send_response(status); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        status = 500 if self.server.broken else (200 if payload.get("password") == "fake-test-password" else 401)
        if status == 200: self.server.authenticated = True
        self.send_response(status)
        self.end_headers()


class FakeModel:
    model_id = "nvidia/Nemotron-3_5-Lightning"
    def __init__(self):
        self.calls = 0
    def complete_json(self, system, user, *, max_output_tokens=800,
                      reasoning_policy=ReasoningPolicy.PROVIDER_DEFAULT):
        self.calls += 1
        if self.calls == 1:
            data = {"conditions": [
                {"id": "C1", "description": "Production responds successfully", "critical": True, "prerequisites": []},
                {"id": "C2", "description": "Valid login reaches dashboard", "critical": True, "prerequisites": []},
                {"id": "C3", "description": "Dashboard content appears after login", "critical": True, "prerequisites": ["C2"]},
                {"id": "C4", "description": "Regression tests pass", "critical": True, "prerequisites": []},
            ]}
        else:
            step_responses = [
                {"steps": [{"verifier": "http", "operation": "status", "target_ref": "production", "route_ref": "home", "expected": 200}]},
                {"steps": [
                    {"verifier": "browser", "operation": "navigate", "target_ref": "production", "route_ref": "login"},
                    {"verifier": "browser", "operation": "fill", "target_ref": "production", "selector_ref": "password", "credential_ref": "valid_test_user.password"},
                    {"verifier": "browser", "operation": "click", "target_ref": "production", "selector_ref": "submit"},
                    {"verifier": "browser", "operation": "wait_for", "target_ref": "production", "selector_ref": "dashboard_heading"},
                    {"verifier": "browser", "operation": "assert_url", "target_ref": "production", "route_ref": "dashboard"},
                ]},
                {"steps": [{"verifier": "browser", "operation": "assert_text", "target_ref": "production", "selector_ref": "dashboard_heading", "expected": "Deployment dashboard"}]},
                {"steps": [{"verifier": "shell", "operation": "pytest", "command_ref": "regression"}]},
            ]
            data = step_responses[self.calls - 2]
        return ModelResponse(data, ModelUsage(self.model_id, 10, 10, .01))


def test_real_failure_repair_reverification(tmp_path):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    (workspace / "test_regression.py").write_text("def test_ok(): assert True\n", encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), ControlledApp)
    server.broken = True
    server.authenticated = False
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    browser = BrowserVerifier()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        context = TrustedContext(production_base_url=base, workspace=str(workspace),
            routes={"home": "/", "login": "/login", "dashboard": "/dashboard"},
            selectors={"password": "#password", "submit": "#submit", "dashboard_heading": "#dashboard-heading"},
            commands={"regression": ("pytest", ("-q",))},
            credentials={"valid_test_user.password": "fake-test-password"})
        model = FakeModel(); workflow = IntelligenceWorkflow(model, context)
        prepared = workflow.prepare("Fix login and deploy")
        engine = VerificationEngine(VerifierRegistry({"http": HTTPVerifier(), "browser": browser, "shell": ShellVerifier()}))
        store = ProofPackStore(tmp_path / "packs")
        first = workflow.verify(prepared, engine, "run_1", artifact_root=str(tmp_path / "artifacts")); store.save(first)
        browser.close("run_1")
        server.broken = False
        second = workflow.verify(prepared, engine, "run_2", artifact_root=str(tmp_path / "artifacts")); store.save(second)
        browser.close("run_2")
        assert first.verdict == "FAILED" and second.verdict == "VERIFIED"
        assert [r.status for r in first.condition_results] == ["VERIFIED", "FAILED", "BLOCKED", "VERIFIED"]
        assert all(r.status == "VERIFIED" for r in second.condition_results)
        assert first.contract_id == second.contract_id and first.contract_version == second.contract_version
        assert first.evidence[0].evidence_id != second.evidence[0].evidence_id
        assert store.load("run_1")["verdict"] == "FAILED" and model.calls == 5
        assert "fake-test-password" not in (tmp_path / "packs" / "run_1.json").read_text()
    finally:
        browser.close("run_1"); browser.close("run_2")
        server.shutdown(); server.server_close(); thread.join(timeout=2)
