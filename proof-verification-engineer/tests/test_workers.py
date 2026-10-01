import sys
from pathlib import Path

import pytest

from app.core.planner import default_contract
from app.evidence.collector import build_report
from app.evidence.models import Evidence
from app.workers.demo import DemoWorkerAdapter
from app.workers.local_coding import CommandRejected, LocalCodingWorkerAdapter, WorkspaceViolation
from app.workers.models import RepairRequest, WorkerTask


def adapter(tmp_path, timeout=2.0):
    return LocalCodingWorkerAdapter(tmp_path / "sandboxes", command_timeout=timeout)


def test_workspace_is_created_and_structured_claim_is_returned(tmp_path):
    worker, task = adapter(tmp_path), WorkerTask("Compile the controlled target")
    result = worker.run_task(task)
    assert Path(result.workspace).is_relative_to(tmp_path / "sandboxes")
    assert (Path(result.workspace) / "worker-task.txt").exists()
    assert result.claim.status == "COMPLETED"
    assert worker.get_result(task.task_id) is result


def test_worker_cannot_escape_workspace(tmp_path):
    worker, workspace = adapter(tmp_path), tmp_path / "sandboxes"
    with pytest.raises(WorkspaceViolation): worker.workspace_path(workspace, "../../outside.txt")


def test_allowed_command_captures_stdout_and_stderr(tmp_path, monkeypatch):
    worker, task = adapter(tmp_path), WorkerTask("Run test")
    workspace = worker.create_workspace(task)
    monkeypatch.setenv("PROOF_API_KEY", "must-not-reach-worker")
    (workspace / "test_output.py").write_text("import os, sys\nprint('worker-out')\nprint('worker-secret=' + str(os.getenv('PROOF_API_KEY')))\nprint('worker-err', file=sys.stderr)\ndef test_ok(): assert True\n")
    result = worker.run_allowed_command(workspace, (sys.executable, "-m", "pytest", "-q", "-s"))
    assert result.exit_code == 0 and "worker-out" in result.stdout and "worker-err" in result.stderr
    assert "worker-secret=None" in result.stdout
    assert result.duration_seconds >= 0


def test_disallowed_command_is_rejected(tmp_path):
    worker, workspace = adapter(tmp_path), tmp_path / "sandboxes"
    with pytest.raises(CommandRejected): worker.run_allowed_command(workspace, ("cmd.exe", "/c", "dir"))


def test_child_environment_excludes_sensitive_parent_values(monkeypatch, tmp_path):
    monkeypatch.setenv("PROOF_API_KEY", "must-not-reach-worker")
    environment = adapter(tmp_path).build_child_environment()
    assert "PROOF_API_KEY" not in environment
    assert environment["PYTHONIOENCODING"] == "utf-8"


def test_command_timeout_is_reported(tmp_path):
    worker, task = adapter(tmp_path, timeout=0.05), WorkerTask("Run test")
    workspace = worker.create_workspace(task)
    (workspace / "test_slow.py").write_text("import time\ndef test_slow(): time.sleep(1)\n")
    result = worker.run_allowed_command(workspace, (sys.executable, "-m", "pytest", "-q"))
    assert result.timed_out and result.exit_code is None


def test_repair_request_is_recorded(tmp_path):
    worker, task = adapter(tmp_path), WorkerTask("Repair target")
    worker.run_task(task)
    repaired = worker.repair_task(RepairRequest(task.task_id, ("HTTP 500",), "Fix the schema"))
    assert any(item.path == "repair-request.txt" for item in repaired.changed_files)
    assert "HTTP 500" in (Path(repaired.workspace) / "repair-request.txt").read_text()


def test_claim_cannot_control_proof_verdict(tmp_path):
    claim = adapter(tmp_path).run_task(WorkerTask("Claim success")).claim
    contract = default_contract("Verify independently")
    evidence = [Evidence(c.id, c.name, "FAIL", c.method, "expected", "observed", {}, "now") for c in contract.conditions]
    assert claim.status == "COMPLETED"
    assert build_report(contract, evidence).verdict == "FAILED"


def test_demo_worker_adapter_remains_available():
    result = DemoWorkerAdapter().run_task(WorkerTask("Fix login"))
    assert result.claim.release == "v1.0-demo"
