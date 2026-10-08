"""Offline v0.5 integration: real loopback app, SQLite defect, worker repair, v0.4 PROOF."""
from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from app import demo_v05
from app.demo_v05 import GOAL, repair_request_from_failed_pack, run_demo
from app.workers.local_coding import CommandRejected, WorkspaceViolation
from app.workers.models import FailedEvidenceReference, RepairRequest, WorkerTask
from app.workers.sqlite_login import SqliteLoginWorkerAdapter


@pytest.fixture(scope="module")
def complete_demo(tmp_path_factory):
    return run_demo(output_root=tmp_path_factory.mktemp("proof-v05-packs"))


@pytest.fixture
def deployed_worker(tmp_path):
    worker = SqliteLoginWorkerAdapter(tmp_path / "workspaces")
    task = WorkerTask(GOAL)
    execution = worker.run_task(task)
    try:
        yield worker, task, execution
    finally:
        worker.close()


def _tables(database: Path) -> set[str]:
    with closing(sqlite3.connect(database)) as connection:
        return {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}


def _login(url: str) -> httpx.Response:
    return httpx.post(url + "/api/login",
                      json={"email": "demo@proof.local", "password": "fake-test-password"},
                      timeout=2)


def test_worker_changes_real_source_and_local_checks_pass(deployed_worker):
    worker, task, execution = deployed_worker
    workspace = Path(execution.workspace)
    source = (workspace / "login_service.py").read_text(encoding="utf-8")
    assert "return candidate == stored" in source
    assert "CONTROLLED_WORKER_FIX" not in source
    assert execution.claim.status == "COMPLETED"
    assert execution.command_results[0].exit_code == 0
    assert execution.command_results[0].duration_seconds > 0
    assert execution.actions[0].name == "fix_local_login_code"
    assert execution.actions[-1].name == "deploy_loopback_application"
    assert worker.production_url(task.task_id).startswith("http://127.0.0.1:")


def test_production_auth_fails_from_actual_missing_table(deployed_worker):
    worker, task, execution = deployed_worker
    database = Path(execution.workspace) / "production.sqlite"
    assert "users" in _tables(database) and "auth_sessions" not in _tables(database)
    response = _login(worker.production_url(task.task_id))
    assert response.status_code == 500
    assert response.json() == {"error": "missing_auth_sessions_table"}
    assert httpx.get(worker.production_url(task.task_id) + "/health/schema", timeout=2).json() == {
        "auth_sessions_exists": False}


def test_worker_claim_does_not_override_failed_independent_evidence(complete_demo):
    result = complete_demo
    assert result.worker_first.claim.status == "COMPLETED"
    assert result.first.verdict == "FAILED"
    assert [item.status for item in result.first.condition_results] == [
        "VERIFIED", "FAILED", "BLOCKED", "VERIFIED"]
    diagnostic = next(item for item in result.first.evidence
                      if item.condition_id == "C2" and item.operation == "json_value")
    assert diagnostic.status == "FAILED"
    assert diagnostic.expected is True and diagnostic.observed is False
    assert result.first.intelligence["worker_claim"]["status"] == "COMPLETED"
    assert result.first.intelligence["model_execution"] == "offline_fixed_responses"


def test_repair_request_references_real_failed_evidence(complete_demo):
    result = complete_demo
    request = result.repair_request
    assert request.instructions == "apply_auth_sessions_migration"
    assert request.evidence == tuple(item.evidence_id for item in request.failed_evidence)
    assert set(request.evidence).issubset({item.evidence_id for item in result.first.evidence})
    assert request.failed_evidence[0].expected == "True"
    assert request.failed_evidence[0].observed == "False"
    with pytest.raises(ValueError):
        repair_request_from_failed_pack(result.second, result.worker_first.task.task_id)


def test_repair_changes_production_database_and_running_behavior(deployed_worker):
    worker, task, execution = deployed_worker
    database = Path(execution.workspace) / "production.sqlite"
    reference = FailedEvidenceReference("evidence-local", "C2", "json_value", "True", "False", "FAILED")
    request = RepairRequest(task.task_id, (reference.evidence_id,),
                            "apply_auth_sessions_migration", (reference,))
    before_url = worker.production_url(task.task_id)
    repaired = worker.repair_task(request)
    assert repaired.claim.status == "COMPLETED"
    assert repaired.actions[0].name == "apply_auth_sessions_migration"
    assert "auth_sessions" in _tables(database)
    assert worker.production_url(task.task_id) == before_url
    assert _login(before_url).status_code == 200
    assert httpx.get(before_url + "/health/schema", timeout=2).json() == {
        "auth_sessions_exists": True}


def test_reverification_reuses_frozen_contract_and_preserves_both_packs(complete_demo):
    result = complete_demo
    assert result.second.verdict == "VERIFIED"
    assert all(item.status == "VERIFIED" for item in result.second.condition_results)
    assert result.first.contract_id == result.second.contract_id
    assert result.first.contract_version == result.second.contract_version == 1
    assert result.first.intelligence["contract"] == result.second.intelligence["contract"]
    assert result.first.intelligence["plan"] == result.second.intelligence["plan"]
    assert result.first.evidence[0].evidence_id != result.second.evidence[0].evidence_id
    assert result.first_path != result.second_path
    assert json.loads(result.first_path.read_text(encoding="utf-8"))["verdict"] == "FAILED"
    assert json.loads(result.second_path.read_text(encoding="utf-8"))["verdict"] == "VERIFIED"
    assert result.model_calls == 5  # One mocked compilation and four step-only plans; none on rerun.
    assert not result.workspace_path.exists()  # Fixture/server cleaned; Proof Packs remain durable.


def test_no_repair_stays_failed(tmp_path):
    result = run_demo(output_root=tmp_path / "packs", perform_repair=False)
    assert result.worker_repair is None
    assert result.first.verdict == result.second.verdict == "FAILED"
    assert result.second.condition_results[2].status == "BLOCKED"
    assert result.model_calls == 5


@pytest.mark.parametrize("request_factory", [
    lambda task: RepairRequest(task.task_id, (), "apply_auth_sessions_migration"),
    lambda task: RepairRequest(task.task_id, ("e1",), "run_arbitrary_sql",
                               (FailedEvidenceReference("e1", "C2", "json_value", "True", "False", "FAILED"),)),
    lambda task: RepairRequest(task.task_id, ("e1",), "apply_auth_sessions_migration",
                               (FailedEvidenceReference("e1", "C2", "json_value", "True", "True", "FAILED"),)),
])
def test_invalid_or_missing_repair_fails_without_changing_database(deployed_worker, request_factory):
    worker, task, execution = deployed_worker
    with pytest.raises(CommandRejected):
        worker.repair_task(request_factory(task))
    assert "auth_sessions" not in _tables(Path(execution.workspace) / "production.sqlite")
    assert _login(worker.production_url(task.task_id)).status_code == 500


def test_incomplete_repair_cannot_claim_verified(monkeypatch, tmp_path):
    monkeypatch.setattr(SqliteLoginWorkerAdapter, "repair_task",
                        lambda self, request: self.get_result(request.task_id))
    root = tmp_path / "packs"
    with pytest.raises(RuntimeError, match="Reverification did not verify"):
        run_demo(output_root=root)
    packs = sorted(root.glob("*.json"))
    assert len(packs) == 2
    assert [json.loads(path.read_text(encoding="utf-8"))["verdict"] for path in packs] == [
        "FAILED", "FAILED"]


def test_workspace_and_command_authority_are_bounded(deployed_worker, tmp_path, monkeypatch):
    worker, task, execution = deployed_worker
    with pytest.raises(WorkspaceViolation):
        worker.workspace_path(Path(execution.workspace), "../../outside.txt")
    with pytest.raises(WorkspaceViolation):
        SqliteLoginWorkerAdapter(Path(__file__).resolve().parent)
    with pytest.raises(CommandRejected):
        worker.run_allowed_command(Path(execution.workspace), ("cmd.exe", "/c", "dir"))
    unassigned = tmp_path / "workspaces" / "unassigned"
    unassigned.mkdir()
    with pytest.raises(WorkspaceViolation):
        worker.run_allowed_command(unassigned, (sys.executable, "-m", "pytest", "-q"))
    with pytest.raises(WorkspaceViolation):
        worker.create_workspace(WorkerTask(GOAL, task_id="../../escape"))
    with pytest.raises(CommandRejected):
        worker.run_allowed_command(Path(execution.workspace),
                                   (sys.executable, "-m", "pytest", "-q", "--override-ini", "foo=bar"))
    monkeypatch.setenv("NEBIUS_API_KEY", "fake-parent-only-key")
    assert "NEBIUS_API_KEY" not in worker.build_child_environment()


def test_proof_packs_and_logs_do_not_contain_credentials(complete_demo):
    result = complete_demo
    for path in (result.first_path, result.second_path):
        text = path.read_text(encoding="utf-8")
        assert "fake-test-password" not in text
        assert "NEBIUS_API_KEY" not in text
    for execution in (result.worker_first, result.worker_repair):
        for command in execution.command_results:
            assert "fake-test-password" not in command.stdout + command.stderr
            assert "fake-parent-only-key" not in command.stdout + command.stderr


def test_cleanup_runs_after_server_start_failure(monkeypatch, tmp_path):
    roots = []
    original = demo_v05.tempfile.TemporaryDirectory
    def track(*args, **kwargs):
        temporary = original(*args, **kwargs)
        roots.append(Path(temporary.name))
        return temporary
    monkeypatch.setattr(demo_v05.tempfile, "TemporaryDirectory", track)
    monkeypatch.setattr("app.workers.sqlite_login.ControlledLoginServer.start",
                        lambda self: (_ for _ in ()).throw(TimeoutError("simulated startup failure")))
    with pytest.raises(TimeoutError, match="simulated startup failure"):
        run_demo(output_root=tmp_path / "packs")
    assert roots and all(not root.exists() for root in roots)


def test_cli_reports_actual_outcome_and_pack_paths(complete_demo, monkeypatch, capsys):
    monkeypatch.setattr(demo_v05, "run_demo", lambda **_kwargs: complete_demo)
    monkeypatch.setattr(sys, "argv", ["app.demo_v05"])
    demo_v05.main()
    output = capsys.readouterr().out
    assert f"WORKER CLAIM: {complete_demo.worker_first.claim.status}" in output
    assert f"PROOF RUN 1: {complete_demo.first.verdict}" in output
    assert f"PROOF RUN 2: {complete_demo.second.verdict}" in output
    assert str(complete_demo.first_path) in output
    assert str(complete_demo.second_path) in output
