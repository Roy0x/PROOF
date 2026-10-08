"""Deterministic coding worker for an explicitly disposable login task workspace."""
from __future__ import annotations

import re
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from time import monotonic

from app.fixtures.local_login_server import ControlledLoginServer

from .local_coding import CommandRejected, LocalCodingWorkerAdapter, WorkspaceViolation
from .models import (ChangedFile, RepairRequest, WorkerAction, WorkerClaim,
                     WorkerExecutionResult, WorkerTask)


class SqliteLoginWorkerAdapter(LocalCodingWorkerAdapter):
    """Controlled worker, not a general-purpose autonomous coding agent."""

    _TEST_COMMAND = (sys.executable, "-m", "pytest", "-q")
    _FIX_POINT = "return False  # CONTROLLED_WORKER_FIX"
    _FIX = "return candidate == stored"
    _MIGRATION = "CREATE TABLE auth_sessions (session_id TEXT PRIMARY KEY, email TEXT NOT NULL)"

    def __init__(self, workspace_root: Path, command_timeout: float = 15.0) -> None:
        resolved = workspace_root.resolve()
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if not resolved.is_relative_to(temporary_root) or resolved == temporary_root:
            raise WorkspaceViolation("Login worker requires an explicitly assigned temporary root")
        super().__init__(resolved, command_timeout)
        self._servers: dict[str, ControlledLoginServer] = {}
        self._assigned: set[Path] = set()

    def create_workspace(self, task: WorkerTask) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task.task_id):
            raise WorkspaceViolation("Invalid task ID")
        workspace = super().create_workspace(task)
        self._assigned.add(workspace)
        return workspace

    def run_allowed_command(self, workspace: Path, command: tuple[str, ...]):
        if workspace.resolve() not in self._assigned:
            raise WorkspaceViolation("Workspace was not assigned to this worker")
        if command != self._TEST_COMMAND:
            raise CommandRejected("Login worker allows only its fixed local pytest command")
        return super().run_allowed_command(workspace, command)

    def run_task(self, task: WorkerTask) -> WorkerExecutionResult:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task.task_id) or task.task_id in self._results:
            raise WorkspaceViolation("Invalid or duplicate task ID")
        workspace = self.create_workspace(task)
        source = self.workspace_path(workspace, "login_service.py")
        test = self.workspace_path(workspace, "test_login_service.py")
        database = self.workspace_path(workspace, "production.sqlite")
        assets = Path(__file__).resolve().parents[1] / "fixtures"
        template = (assets / "login_service.py.txt").read_text(encoding="utf-8")
        if template.count(self._FIX_POINT) != 1:
            raise RuntimeError("Controlled source fix point is missing or ambiguous")
        source.write_text(template, encoding="utf-8")
        started = monotonic()
        source.write_text(template.replace(self._FIX_POINT, self._FIX), encoding="utf-8")
        code_action = WorkerAction("fix_local_login_code", "login_service.py", "applied",
                                   monotonic() - started)
        test.write_text((assets / "test_login_service.py.txt").read_text(encoding="utf-8"), encoding="utf-8")
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE users (email TEXT PRIMARY KEY, password TEXT NOT NULL)")
            connection.execute("INSERT INTO users VALUES (?, ?)",
                               ("demo@proof.local", "fake-test-password"))
            connection.commit()
        result = self.run_allowed_command(workspace, self._TEST_COMMAND)
        changed = [ChangedFile("login_service.py", "Fixed the local password comparison."),
                   ChangedFile("test_login_service.py", "Added local SQLite login regression tests."),
                   ChangedFile("production.sqlite", "Prepared separate production database; migration omitted.")]
        actions = [code_action, WorkerAction("run_local_tests", "test_login_service.py",
                                             "passed" if result.exit_code == 0 else "failed",
                                             result.duration_seconds)]
        if result.exit_code != 0 or result.timed_out:
            execution = WorkerExecutionResult(task, str(workspace),
                WorkerClaim("FAILED", "Local regression checks did not pass.", task.task_id),
                changed, [result], actions=actions)
            self._results[task.task_id] = execution
            return execution
        started = monotonic()
        server = ControlledLoginServer(workspace, database)
        try:
            server.start()
        except Exception:
            server.close()
            raise
        self._servers[task.task_id] = server
        actions.append(WorkerAction("deploy_loopback_application", server.base_url,
                                    "running", monotonic() - started))
        execution = WorkerExecutionResult(task, str(workspace),
            WorkerClaim("COMPLETED", "Local tests passed and the controlled release is running.",
                        task.task_id, "local-v0.5"), changed, [result], actions=actions)
        self._results[task.task_id] = execution
        return execution

    def production_url(self, task_id: str) -> str:
        if task_id not in self._servers:
            raise KeyError("Task has no running controlled deployment")
        return self._servers[task_id].base_url

    def repair_task(self, request: RepairRequest) -> WorkerExecutionResult:
        original = self._results.get(request.task_id)
        if original is None or request.task_id not in self._servers:
            raise KeyError("Unknown or undeployed worker task")
        if (request.instructions != "apply_auth_sessions_migration"
                or not request.failed_evidence
                or tuple(item.evidence_id for item in request.failed_evidence) != request.evidence
                or not any(item.condition_id == "C2" and item.operation == "json_value"
                           and item.expected == "True" and item.observed == "False"
                           and item.status == "FAILED"
                           for item in request.failed_evidence)):
            raise CommandRejected("Repair is not authorized by failed production-login evidence")
        workspace = Path(original.workspace)
        database = self.workspace_path(workspace, "production.sqlite")
        if not database.is_file():
            raise WorkspaceViolation("Assigned production database is missing")
        started = monotonic()
        with closing(sqlite3.connect(database)) as connection:
            users = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
            sessions = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='auth_sessions'").fetchone()
            if not users or sessions:
                raise CommandRejected("Production migration precondition is not met")
            connection.execute(self._MIGRATION)
            connection.commit()
        action = WorkerAction("apply_auth_sessions_migration", "production.sqlite",
                              "applied", monotonic() - started)
        result = self.run_allowed_command(workspace, self._TEST_COMMAND)
        claim = WorkerClaim("COMPLETED" if result.exit_code == 0 else "FAILED",
                            "Controlled production migration applied; local checks rerun.",
                            request.task_id, "local-v0.5-repaired")
        execution = WorkerExecutionResult(original.task, original.workspace, claim,
            [ChangedFile("production.sqlite", "Applied auth_sessions migration to disposable production database.")],
            [result], actions=[action, WorkerAction("run_local_tests", "test_login_service.py",
                                                  "passed" if result.exit_code == 0 else "failed",
                                                  result.duration_seconds)])
        self._results[request.task_id] = execution
        return execution

    def close(self) -> None:
        for server in self._servers.values():
            server.close()
        self._servers.clear()
