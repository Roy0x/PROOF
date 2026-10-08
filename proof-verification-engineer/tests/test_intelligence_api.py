from fastapi.testclient import TestClient

from app.main import app
from app.llm.token_factory import ModelResponse, ModelUsage, ReasoningPolicy

GOAL = "Confirm that production is reachable"
CONTRACT = {"conditions": [{"id": "C1", "description": "Production responds with HTTP 200",
                            "critical": True, "prerequisites": []}]}
PLAN = {"steps": [{"verifier": "http", "operation": "status",
         "target_ref": "production", "route_ref": "home", "expected": 200}]}


class FakeClient:
    model_id = "nvidia/Nemotron-3_5-Lightning"
    def __init__(self):
        self.responses = [CONTRACT, PLAN]
    def complete_json(self, system, user, *, max_output_tokens=800,
                      reasoning_policy=ReasoningPolicy.PROVIDER_DEFAULT):
        return ModelResponse(self.responses.pop(0), ModelUsage(self.model_id, 1, 1, .01))


def test_prepare_endpoint_uses_server_context_and_returns_structured_plan(monkeypatch, tmp_path):
    monkeypatch.setenv("PROOF_PRODUCTION_BASE_URL", "http://127.0.0.1:8080/demo-app")
    monkeypatch.setenv("PROOF_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("PROOF_TEST_PASSWORD", "fake-password")
    monkeypatch.setattr("app.api.intelligence.TokenFactoryClient", FakeClient)
    response = TestClient(app).post("/api/v0.4/prepare", json={"goal": GOAL})
    assert response.status_code == 200
    body = response.json()
    assert body["contract"]["goal"] == GOAL
    assert body["plan"]["contract_id"] == body["contract"]["contract_id"]
    assert "fake-password" not in response.text


def test_prepare_endpoint_missing_key_fails_without_fallback(monkeypatch):
    monkeypatch.setenv("PROOF_PRODUCTION_BASE_URL", "https://example.com")
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    response = TestClient(app).post("/api/v0.4/prepare", json={"goal": GOAL})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "missing_api_key"
    assert "contract" not in response.json()


def test_prepare_endpoint_invalid_provider_url_is_structured_error(monkeypatch):
    monkeypatch.setenv("PROOF_PRODUCTION_BASE_URL", "https://example.com")
    monkeypatch.setenv("NEBIUS_BASE_URL", "file:///secret")
    response = TestClient(app).post("/api/v0.4/prepare", json={"goal": GOAL})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "invalid_configuration"
