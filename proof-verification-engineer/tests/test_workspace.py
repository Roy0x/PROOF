"""Offline-only checks for the v0.8 presentation boundary."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app.api import workspace as workspace_api
from app.core.workspace_service import WorkspaceRunService
from app.main import app


def _wait(service: WorkspaceRunService, job_id: str, timeout: float = 120) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        item = service.get(job_id)
        if item["status"] in ("complete", "error", "timeout"):
            return item
        time.sleep(0.1)
    pytest.fail("Offline workspace run exceeded its test deadline")


@pytest.fixture
def service(tmp_path, monkeypatch):
    def no_provider(*args, **kwargs):
        pytest.fail("Workspace attempted a Nebius inference call")

    monkeypatch.setattr("app.llm.token_factory.TokenFactoryClient.complete_json", no_provider)
    instance = WorkspaceRunService(root=tmp_path / "packs")
    monkeypatch.setattr(workspace_api, "workspace_service", instance)
    yield instance
    instance.close()


def test_real_offline_workspace_failed_then_verified_with_persisted_evidence(service):
    client = TestClient(app)
    assert client.get("/").status_code == 200
    assert client.post("/api/workspace/runs").status_code == 403
    started = client.post("/api/workspace/runs", headers={"X-PROOF-Workspace": "offline"})
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    assert started.json()["execution_mode"] == "offline_mock"
    assert started.json()["pack_ids"] == []
    assert client.get("/api/workspace/runs/current").json()["job_id"] == job_id
    final = _wait(service, job_id)
    assert final["status"] == "complete", final["error_code"]
    assert final["worker_claim"] == "COMPLETED"
    assert [event["stage"] for event in final["events"]] == [
        "worker_running", "worker_completed", "contract_frozen", "plan_validated",
        "verification_running", "first_pack_saved", "failed_evidence_confirmed",
        "repair_running", "repair_applied", "reverification_running", "second_pack_saved",
    ]
    assert len(final["pack_ids"]) == 2
    first, second = [client.get(f"/api/workspace/runs/{job_id}/packs/{run_id}").json()
                     for run_id in final["pack_ids"]]
    assert (first["verdict"], second["verdict"]) == ("FAILED", "VERIFIED")
    assert first["redacted"] is second["redacted"] is True
    assert first["contract_id"] == second["contract_id"] == final["contract_id"]
    assert first["contract_version"] == second["contract_version"]
    assert first["plan_fingerprint"] == second["plan_fingerprint"] == final["plan_fingerprint"]
    assert {item["evidence_id"] for item in first["evidence"]}.isdisjoint(
        {item["evidence_id"] for item in second["evidence"]})
    assert {item["acceptance_ref"] for item in first["contract"]["conditions"]} == {
        "home", "auth", "dashboard", "schema", "regression"}
    schema_id = next(item["id"] for item in first["contract"]["conditions"]
                     if item["acceptance_ref"] == "schema")
    failed = next(item for item in first["evidence"]
                  if item["condition_id"] == schema_id and item["operation"] == "json_value")
    passed = next(item for item in second["evidence"]
                  if item["condition_id"] == schema_id and item["operation"] == "json_value")
    assert (failed["expected"], failed["observed"], failed["status"]) == (True, False, "FAILED")
    assert (passed["expected"], passed["observed"], passed["status"]) == (True, True, "VERIFIED")
    assert failed["evidence_id"] in second["repair"]["evidence_ids"]
    assert second["repair"]["migration_ref"] == "auth_sessions_v1"
    assert second["repair"]["outcome"] == "applied"
    assert first["worker"]["claim"] == "COMPLETED"
    assert first["usage"]["provider_requests_attempted"] == 0
    assert second["usage"]["provider_requests_attempted"] == 0
    raw_path = service.root / job_id / f"{first['run_id']}.json"
    assert first["source_sha256"] == hashlib.sha256(raw_path.read_bytes()).hexdigest()
    exported = client.get(f"/api/workspace/runs/{job_id}/packs/{first['run_id']}/export")
    assert exported.status_code == 200
    assert json.loads(exported.content) == first
    assert "attachment" in exported.headers["content-disposition"]
    serialized = json.dumps([first, second, final, exported.json()])
    for forbidden in ("NEBIUS_API_KEY", "fake-test-password", "stdout", "body_excerpt",
                      "screenshot_path", "Authorization", "C:\\Users\\"):
        assert forbidden not in serialized
    assert raw_path.read_bytes() == (service.root / job_id / f"{first['run_id']}.json").read_bytes()


def test_invalid_ids_and_pack_tampering_fail_closed(service):
    client = TestClient(app)
    assert client.get("/api/workspace/runs/not-an-id").status_code == 404
    assert client.get("/api/workspace/runs/../packs").status_code in (404, 405)
    assert client.get("/api/workspace/runs/" + "a" * 32 + "/packs/../../x").status_code in (404, 405)
    job_id = service.start()["job_id"]
    assert client.get(f"/api/workspace/runs/{job_id}/packs/{'a' * 32}_1").status_code == 404
    final = _wait(service, job_id)
    assert final["status"] == "complete", final["error_code"]
    run_id = final["pack_ids"][0]
    pack_path = service.root / job_id / f"{run_id}.json"
    pack_path.write_text(pack_path.read_text() + " ", encoding="utf-8")
    assert client.get(f"/api/workspace/runs/{job_id}/packs/{run_id}").status_code == 409


def test_concurrent_run_prevention_and_incomplete_error_state(tmp_path, monkeypatch):
    entered, release = Event(), Event()

    def suspended_runner(root: Path, progress):
        progress("worker_running", {})
        entered.set()
        assert release.wait(10)
        raise ValueError("a private provider response must not be exposed")

    service = WorkspaceRunService(suspended_runner, root=tmp_path / "blocked")
    monkeypatch.setattr(workspace_api, "workspace_service", service)
    client = TestClient(app)
    try:
        first_response = client.post("/api/workspace/runs", headers={"X-PROOF-Workspace": "offline"})
        assert first_response.status_code == 202
        first = first_response.json()
        assert entered.wait(5)
        assert client.get(f"/api/workspace/runs/{first['job_id']}").json()["stage"] == "worker_running"
        assert client.post("/api/workspace/runs", headers={"X-PROOF-Workspace": "offline"}).status_code == 409
        assert service.get(first["job_id"])["pack_ids"] == []
        release.set()
        final = _wait(service, first["job_id"], 10)
        assert final["status"] == "error"
        assert final["error_code"] == "run_failed"
        assert "private provider" not in json.dumps(final)
        assert final["pack_ids"] == []
        assert client.get(f"/api/workspace/runs/{first['job_id']}").json()["status"] == "error"
    finally:
        release.set()
        service.close()


def test_timeout_keeps_worker_exclusive_until_it_exits(tmp_path):
    entered, release = Event(), Event()

    def slow_runner(root: Path, progress):
        progress("worker_running", {})
        entered.set()
        assert release.wait(10)
        raise RuntimeError("late failure")

    service = WorkspaceRunService(slow_runner, root=tmp_path / "timeout", timeout_seconds=0.01)
    try:
        job_id = service.start()["job_id"]
        assert entered.wait(5)
        time.sleep(0.03)
        timed_out = service.get(job_id)
        assert timed_out["status"] == timed_out["error_code"] == "timeout"
        assert timed_out["worker_active"] is True
        with pytest.raises(Exception, match="already active"):
            service.start()
        release.set()
        end = time.monotonic() + 5
        while service.get(job_id)["worker_active"] and time.monotonic() < end:
            time.sleep(0.01)
        assert service.get(job_id)["status"] == "timeout"
        assert service.get(job_id)["worker_active"] is False
    finally:
        release.set()
        service.close()


@pytest.mark.parametrize("failure, expected", [
    (InterruptedError("stopped"), "interrupted"),
    (TimeoutError("deadline"), "timeout"),
])
def test_runner_interruption_and_timeout_are_reported(tmp_path, failure, expected):
    def runner(root: Path, progress):
        raise failure

    service = WorkspaceRunService(runner, root=tmp_path / expected)
    try:
        final = _wait(service, service.start()["job_id"], 5)
        assert final["error_code"] == expected
        assert final["status"] == ("timeout" if expected == "timeout" else "error")
        assert final["pack_ids"] == []
    finally:
        service.close()


def test_legacy_live_model_routes_are_disabled_by_default(monkeypatch):
    monkeypatch.delenv("PROOF_ENABLE_LEGACY_LIVE_API", raising=False)
    client = TestClient(app)
    assert client.post("/api/contracts/generate", json={"goal": "test"}).status_code == 403
    assert client.post("/api/v0.4/prepare", json={"goal": "test"}).status_code == 403


def test_workspace_frontend_uses_only_offline_evidence_api():
    static = Path(__file__).resolve().parents[1] / "app" / "static"
    html = (static / "workspace.html").read_text(encoding="utf-8")
    script = (static / "workspace.js").read_text(encoding="utf-8")
    assert "/static/workspace.js" in html
    assert "/api/workspace/runs" in script
    assert '"X-PROOF-Workspace": "offline"' in script
    assert "/api/demo/run" not in script
    assert "--live" not in script
    assert "NEBIUS_API_KEY" not in html + script
