"""v0.6 worker decisions are mocked; local subprocesses/verifiers remain real."""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

import httpx
import pytest

from app import demo_v06
from app.demo_v06 import OfflineWorkerModel, repair_request_from_proof, run_demo
from app.llm.token_factory import ModelError, ReasoningPolicy, TokenFactoryClient
from app.workers.local_coding import CommandRejected, WorkspaceViolation
from app.workers.models import FailedEvidenceReference, RepairRequest, WorkerTask
from app.workers.nemotron_coding import (CodePatchDecision, NemotronCodingWorkerAdapter,
                                         WorkerDecisionError, _safe_expression)


@pytest.fixture(scope="module")
def complete_demo(tmp_path_factory):
    return run_demo(output_root=tmp_path_factory.mktemp("proof-v06-packs"))


def _worker(tmp_path, responses=None):
    model = OfflineWorkerModel(responses)
    return NemotronCodingWorkerAdapter(tmp_path / "workspaces", model), model


def _tables(path: Path):
    with closing(sqlite3.connect(path)) as connection:
        return {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_model_chosen_patch_is_real_and_local_failure_precedes_success(complete_demo):
    result = complete_demo
    checks = result.worker_first.command_results
    assert len(checks) == 2 and checks[0].exit_code != 0 and checks[1].exit_code == 0
    assert "test_valid_login_creates_session" in checks[0].stdout
    assert result.worker_first.claim.status == "COMPLETED"
    assert result.worker_first.actions[0].name == "propose_code_patch"
    assert result.first.intelligence["worker_model_decisions"][0]["selected_value"] == "candidate == stored"
    assert result.worker_model_calls == 2 and result.proof_model_calls == 5
    assert not result.workspace_path.exists()


def test_different_model_choice_changes_source_and_does_not_deploy(tmp_path):
    wrong = {"action": "propose_code_patch", "file": "login_service.py",
             "function": "password_matches", "replacement_expression": "candidate != stored"}
    worker, model = _worker(tmp_path, (wrong,))
    try:
        result = worker.run_task(WorkerTask("Fix login"))
        assert "return candidate != stored" in (Path(result.workspace) / "login_service.py").read_text()
        assert result.command_results[1].exit_code != 0
        assert result.claim.status == "FAILED"
        assert worker.model_calls == 1
        assert model.calls[0]["reasoning_policy"] is ReasoningPolicy.DISABLED
        assert model.calls[0]["max_output_tokens"] == 256
        observation = json.loads(model.calls[0]["user"])
        assert "CONTROLLED_WORKER_FIX" in observation["relevant_source"]
        assert observation["local_test"]["failed_tests"]
        with pytest.raises(KeyError):
            worker.production_url(result.task.task_id)
    finally:
        worker.close()


@pytest.mark.parametrize("expression", [
    "__import__('os').system('whoami')", "candidate.__class__", "open('x')",
    "candidate == stored; print('x')", "[candidate for _ in stored]",
    "True", "candidate == stored or True", "candidate[0] == stored[0]",
])
def test_unsafe_expressions_rejected(expression):
    with pytest.raises(WorkerDecisionError, match="unsafe_patch"):
        _safe_expression(expression)


@pytest.mark.parametrize("change", [
    {"file": "../login_service.py"}, {"file": "production.sqlite"},
    {"function": "authenticate"}, {"shell": "powershell.exe"},
    {"sql": "DROP TABLE users"}, {"overall_verdict": "VERIFIED"},
])
def test_unauthorized_patch_fields_fail_closed(tmp_path, change):
    base = {"action": "propose_code_patch", "file": "login_service.py",
            "function": "password_matches", "replacement_expression": "candidate == stored"}
    worker, _ = _worker(tmp_path, ({**base, **change},))
    try:
        with pytest.raises(WorkerDecisionError, match="invalid_model_action"):
            worker.run_task(WorkerTask("Fix login"))
        assert worker.model_calls == 1
        assert not list((tmp_path / "workspaces").rglob("production.sqlite"))
    finally:
        worker.close()


def test_malformed_action_and_provider_error_are_sanitized(tmp_path):
    worker, _ = _worker(tmp_path / "malformed", ({"action": "propose_code_patch"},))
    try:
        with pytest.raises(WorkerDecisionError, match="invalid_model_action"):
            worker.run_task(WorkerTask("Fix login"))
    finally:
        worker.close()

    class FailingModel:
        model_id = "offline/failure"
        def complete_json(self, *_args, **_kwargs):
            raise ModelError("provider_unavailable", "provider response containing secret-value")

    worker = NemotronCodingWorkerAdapter(tmp_path / "provider" / "workspaces", FailingModel())
    try:
        with pytest.raises(WorkerDecisionError, match="provider_unavailable") as error:
            worker.run_task(WorkerTask("Fix login"))
        assert "secret-value" not in str(error.value)
    finally:
        worker.close()


def test_missing_key_fails_before_network(tmp_path):
    client = TokenFactoryClient(api_key="", retries=0)
    worker = NemotronCodingWorkerAdapter(tmp_path / "workspaces", client)
    try:
        with pytest.raises(WorkerDecisionError, match="missing_api_key"):
            worker.run_task(WorkerTask("Fix login"))
        assert worker.model_calls == 1
    finally:
        worker.close()


def test_child_process_env_excludes_nebius_key(tmp_path, monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "parent-only-secret")
    worker, _ = _worker(tmp_path)
    try:
        assert "NEBIUS_API_KEY" not in worker.build_child_environment()
        original = worker.run_allowed_command
        def checked(workspace, command):
            assert "NEBIUS_API_KEY" not in worker.build_child_environment()
            return original(workspace, command)
        monkeypatch.setattr(worker, "run_allowed_command", checked)
        result = worker.run_task(WorkerTask("Fix login"))
        assert result.claim.status == "COMPLETED"
    finally:
        worker.close()


def test_worker_workspace_and_command_allowlist(tmp_path):
    worker, _ = _worker(tmp_path)
    try:
        result = worker.run_task(WorkerTask("Fix login"))
        workspace = Path(result.workspace)
        with pytest.raises(WorkspaceViolation):
            worker.workspace_path(workspace, "../../outside.py")
        with pytest.raises(CommandRejected):
            worker.run_allowed_command(workspace, ("cmd.exe", "/c", "dir"))
        with pytest.raises(WorkspaceViolation):
            worker.create_workspace(WorkerTask("x", task_id="../../escape"))
    finally:
        worker.close()


def test_first_proof_failure_and_grounded_repair_request(complete_demo):
    result = complete_demo
    assert result.first.verdict == "FAILED"
    assert [item.status for item in result.first.condition_results] == [
        "VERIFIED", "FAILED", "BLOCKED", "VERIFIED"]
    reference = result.repair_request.failed_evidence[0]
    assert reference.evidence_id in {item.evidence_id for item in result.first.evidence}
    assert (reference.condition_id, reference.operation, reference.expected, reference.observed) == (
        "C2", "json_value", "True", "False")
    assert result.first.intelligence["worker_claim"]["status"] == "COMPLETED"
    with pytest.raises(ValueError):
        repair_request_from_proof(result.second, result.worker_first.task.task_id)


@pytest.mark.parametrize("bad_repair", [
    {"action": "apply_authorized_migration", "migration_ref": "drop_users"},
    {"action": "run_sql", "sql": "CREATE TABLE auth_sessions(x)"},
    {"action": "apply_authorized_migration", "migration_ref": "auth_sessions_v1", "shell": "cmd.exe"},
    {"action": "apply_authorized_migration", "migration_ref": "auth_sessions_v1", "overall_verdict": "VERIFIED"},
])
def test_invalid_repair_does_not_modify_db_or_verify(tmp_path, bad_repair):
    good_patch = {"action": "propose_code_patch", "file": "login_service.py",
                  "function": "password_matches", "replacement_expression": "candidate == stored"}
    worker, _ = _worker(tmp_path, (good_patch, bad_repair))
    try:
        first = worker.run_task(WorkerTask("Fix login"))
        db = Path(first.workspace) / "production.sqlite"
        reference = FailedEvidenceReference("evidence-1", "C2", "json_value", "True", "False", "FAILED")
        request = RepairRequest(first.task.task_id, (reference.evidence_id,),
                                "choose_repair_from_failed_proof_evidence", (reference,))
        with pytest.raises(WorkerDecisionError, match="invalid_model_action"):
            worker.repair_task(request)
        assert "auth_sessions" not in _tables(db)
        assert httpx.post(worker.production_url(first.task.task_id) + "/api/login",
                          json={"email": "demo@proof.local", "password": "fake-test-password"},
                          timeout=2).status_code == 500
    finally:
        worker.close()


def test_invalid_repair_request_rejected_before_model_call(tmp_path):
    worker, model = _worker(tmp_path)
    try:
        first = worker.run_task(WorkerTask("Fix login"))
        with pytest.raises(CommandRejected):
            worker.repair_task(RepairRequest(first.task.task_id, (), "run_arbitrary_sql"))
        assert worker.model_calls == 1 and len(model.calls) == 1
    finally:
        worker.close()


def test_model_selected_repair_changes_real_sqlite_state(tmp_path):
    worker, model = _worker(tmp_path)
    try:
        first = worker.run_task(WorkerTask("Fix login"))
        db = Path(first.workspace) / "production.sqlite"
        assert "auth_sessions" not in _tables(db)
        reference = FailedEvidenceReference("evidence-1", "C2", "json_value", "True", "False", "FAILED")
        request = RepairRequest(first.task.task_id, (reference.evidence_id,),
                                "choose_repair_from_failed_proof_evidence", (reference,))
        repaired = worker.repair_task(request)
        assert repaired.claim.status == "COMPLETED"
        assert "auth_sessions" in _tables(db)
        assert worker.decision_records[-1].selected_value == "auth_sessions_v1"
        assert len(model.calls) == 2
        response = httpx.post(worker.production_url(first.task.task_id) + "/api/login",
                              json={"email": "demo@proof.local", "password": "fake-test-password"}, timeout=2)
        assert response.status_code == 200
    finally:
        worker.close()


def test_failed_repair_keeps_only_first_failed_pack(tmp_path):
    patch = {"action": "propose_code_patch", "file": "login_service.py",
             "function": "password_matches", "replacement_expression": "candidate == stored"}
    model = OfflineWorkerModel((patch, {"action": "run_sql", "sql": "DROP TABLE users"}))
    root = tmp_path / "packs"
    with pytest.raises(WorkerDecisionError, match="invalid_model_action"):
        run_demo(output_root=root, worker_model=model)
    packs = list(root.glob("*.json"))
    assert len(packs) == 1
    assert json.loads(packs[0].read_text())["verdict"] == "FAILED"
    assert len(model.calls) == 2


def test_valid_repair_changes_sqlite_and_fresh_proof_preserves_first_pack(complete_demo):
    result = complete_demo
    assert result.worker_repair.actions[0].name == "apply_authorized_migration"
    assert result.second.verdict == "VERIFIED"
    assert result.first.contract_id == result.second.contract_id
    assert result.first.contract_version == result.second.contract_version
    assert result.first.intelligence["contract"] == result.second.intelligence["contract"]
    assert result.first.intelligence["plan"] == result.second.intelligence["plan"]
    assert {item.evidence_id for item in result.first.evidence}.isdisjoint(
        {item.evidence_id for item in result.second.evidence})
    assert json.loads(result.first_path.read_text())["verdict"] == "FAILED"
    assert json.loads(result.second_path.read_text())["verdict"] == "VERIFIED"
    assert len(result.first.intelligence["worker_model_decisions"]) == 1
    assert len(result.second.intelligence["worker_model_decisions"]) == 2
    assert result.first.intelligence["model_execution"] == "offline_mock"


def test_proof_packs_exclude_secrets_and_raw_prompts(complete_demo):
    for path in (complete_demo.first_path, complete_demo.second_path):
        content = path.read_text()
        for forbidden in ("fake-test-password", "NEBIUS_API_KEY", "Authorization", "replacement_expression", "output_excerpt"):
            assert forbidden not in content
    for item in complete_demo.second.intelligence["worker_model_decisions"]:
        assert "prompt_version" in item and "model_id" in item
        assert "user" not in item and "system" not in item


def test_demo_observation_exposes_only_failed_evidence_to_repair_model(tmp_path):
    model = OfflineWorkerModel()
    result = run_demo(output_root=tmp_path / "packs", worker_model=model)
    assert len(model.calls) == 2
    repair = json.loads(model.calls[1]["user"])
    assert repair["available_migrations"] == ["auth_sessions_v1"]
    assert repair["failed_evidence"][0]["evidence_id"] == result.repair_request.evidence[0]
    assert repair["failed_evidence"][0]["expected"] == "True"
    assert repair["failed_evidence"][0]["observed"] == "False"
    assert "fake-test-password" not in model.calls[1]["user"]
    assert model.calls[1]["reasoning_policy"] is ReasoningPolicy.DISABLED


def test_live_mode_requires_explicit_opt_in(monkeypatch, tmp_path):
    monkeypatch.delenv("PROOF_RUN_LIVE_AI_WORKER", raising=False)
    with pytest.raises(ValueError, match="PROOF_RUN_LIVE_AI_WORKER"):
        run_demo(output_root=tmp_path / "packs", live=True)


def test_offline_model_never_constructs_live_client(monkeypatch, tmp_path):
    monkeypatch.setattr(demo_v06, "TokenFactoryClient",
                        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected Nebius client")))
    result = run_demo(output_root=tmp_path / "packs")
    assert result.model_execution == "offline_mock"


def test_cli_reports_dynamic_results(monkeypatch, capsys, complete_demo):
    monkeypatch.setattr(demo_v06, "run_demo", lambda **_kwargs: complete_demo)
    monkeypatch.setattr(sys, "argv", ["app.demo_v06"])
    demo_v06.main()
    output = capsys.readouterr().out
    assert f"PROOF RUN 1: {complete_demo.first.verdict}" in output
    assert f"PROOF RUN 2: {complete_demo.second.verdict}" in output
    assert str(complete_demo.first_path) in output
    assert str(complete_demo.second_path) in output


def test_safe_expression_positive_and_strict_schema():
    assert _safe_expression("candidate == stored") == "candidate == stored"
    assert _safe_expression("candidate != stored") == "candidate != stored"
    assert CodePatchDecision.model_validate({"action": "propose_code_patch", "file": "login_service.py",
        "function": "password_matches", "replacement_expression": "candidate == stored"})
