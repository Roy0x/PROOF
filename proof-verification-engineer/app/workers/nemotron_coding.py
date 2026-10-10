"""Two-decision, narrowly authorized Nemotron worker for the disposable login fixture.

The model chooses an expression and a migration reference. Trusted Python code alone
edits the assigned file and executes the fixed migration; this is not a host sandbox.
"""
from __future__ import annotations

import ast
import json
import re
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from time import monotonic
from typing import Literal

from pydantic import Field, ValidationError

from app.core.intelligence import StrictModel
from app.fixtures.local_login_server import ControlledLoginServer
from app.llm.token_factory import ModelClient, ModelError, ModelUsage, ReasoningPolicy

from .local_coding import CommandRejected, WorkspaceViolation
from .models import ChangedFile, CommandResult, RepairRequest, WorkerAction, WorkerClaim, WorkerExecutionResult, WorkerTask
from .sqlite_login import SqliteLoginWorkerAdapter


PATCH_PROMPT_VERSION = "coding_patch_v1"
REPAIR_PROMPT_VERSION = "coding_repair_v1"
WORKER_OUTPUT_TOKENS = 256
MAX_DECISIONS = 2
PATCH_SYSTEM = ("Return one JSON object only: action=propose_code_patch, file=login_service.py, "
                "function=password_matches, replacement_expression=a Python expression using only "
                "candidate and stored with == or !=. You may replace ONLY the return expression in "
                "password_matches. No code blocks, imports, calls, explanations, or verdicts. "
                "Use the failing local test to choose the repair.")
REPAIR_SYSTEM = ("Return one JSON object only: action=apply_authorized_migration, "
                 "migration_ref=auth_sessions_v1 if failed PROOF evidence shows the production "
                 "auth_sessions table is absent. Choose only this trusted migration reference; "
                 "never supply SQL, shell commands, explanations, or a verdict.")


class WorkerDecisionError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"Worker decision failed ({code})")


class CodePatchDecision(StrictModel):
    action: Literal["propose_code_patch"]
    file: Literal["login_service.py"]
    function: Literal["password_matches"]
    replacement_expression: str = Field(min_length=1, max_length=120)


class MigrationDecision(StrictModel):
    action: Literal["apply_authorized_migration"]
    migration_ref: Literal["auth_sessions_v1"]


@dataclass(frozen=True)
class DecisionRecord:
    action: str
    target: str
    prompt_version: str
    model_id: str
    prompt_tokens: int | None
    completion_tokens: int | None
    reasoning_tokens: int | None
    latency_seconds: float
    request_count: int
    selected_value: str


def _safe_expression(expression: str) -> str:
    """Validate a non-executable expression tree before writing fixture source."""
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        raise WorkerDecisionError("unsafe_patch") from None
    node = tree.body
    if not (isinstance(node, ast.Compare) and len(node.ops) == 1
            and type(node.ops[0]) in (ast.Eq, ast.NotEq)
            and isinstance(node.left, ast.Name) and node.left.id in ("candidate", "stored")
            and len(node.comparators) == 1 and isinstance(node.comparators[0], ast.Name)
            and node.comparators[0].id in ("candidate", "stored")):
        raise WorkerDecisionError("unsafe_patch")
    return ast.unparse(node)


def _replace_password_expression(source: str, expression: str) -> str:
    tree = ast.parse(source)
    matches = [node for node in tree.body if isinstance(node, ast.FunctionDef)
               and node.name == "password_matches"]
    if len(matches) != 1 or len(matches[0].body) != 1 or not isinstance(matches[0].body[0], ast.Return):
        raise WorkerDecisionError("source_shape_changed")
    return_node = matches[0].body[0]
    lines = source.splitlines(keepends=True)
    if return_node.lineno != return_node.end_lineno or lines[return_node.lineno - 1].strip() != "return False  # CONTROLLED_WORKER_FIX":
        raise WorkerDecisionError("source_shape_changed")
    lines[return_node.lineno - 1] = "    return " + _safe_expression(expression) + "\n"
    return "".join(lines)


def _safe_test_observation(result: CommandResult) -> dict:
    # Local fixture output is untrusted; do not ship entire pytest tracebacks or fake passwords.
    output = (result.stdout + "\n" + result.stderr).replace("fake-test-password", "[redacted]")
    failed = sorted(set(re.findall(r"(?:FAILED|ERROR) (test_login_service\.py::[A-Za-z0-9_]+)", output)))
    return {"exit_code": result.exit_code, "timed_out": result.timed_out,
            "failed_tests": failed[:8], "output_excerpt": output[:1600]}


class NemotronCodingWorkerAdapter(SqliteLoginWorkerAdapter):
    """Separate v0.6 adapter; inherits only v0.5 workspace and command restrictions."""

    def __init__(self, workspace_root: Path, model: ModelClient, command_timeout: float = 15.0) -> None:
        super().__init__(workspace_root, command_timeout)
        self._model = model
        self._decision_count = 0
        self.decision_records: list[DecisionRecord] = []

    @property
    def model_calls(self) -> int:
        return self._decision_count

    def _decide(self, system: str, observation: dict, schema, version: str):
        if self._decision_count >= MAX_DECISIONS:
            raise WorkerDecisionError("decision_budget_exhausted")
        self._decision_count += 1  # charge attempts, including failures
        try:
            response = self._model.complete_json(system, json.dumps(observation, sort_keys=True),
                                                 max_output_tokens=WORKER_OUTPUT_TOKENS,
                                                 reasoning_policy=ReasoningPolicy.DISABLED)
        except ModelError as exc:
            raise WorkerDecisionError(exc.code) from None
        try:
            decision = schema.model_validate(response.data)
        except ValidationError:
            raise WorkerDecisionError("invalid_model_action") from None
        return decision, response.usage

    def _record(self, action: str, target: str, selected: str, version: str, usage: ModelUsage) -> None:
        self.decision_records.append(DecisionRecord(action, target, version, usage.model_id,
            usage.prompt_tokens, usage.completion_tokens, usage.reasoning_tokens,
            usage.latency_seconds, usage.request_count, selected))

    def run_task(self, task: WorkerTask) -> WorkerExecutionResult:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", task.task_id) or task.task_id in self._results:
            raise WorkspaceViolation("Invalid or duplicate task ID")
        workspace = self.create_workspace(task)
        assets = Path(__file__).resolve().parents[1] / "fixtures"
        source = self.workspace_path(workspace, "login_service.py")
        test = self.workspace_path(workspace, "test_login_service.py")
        database = self.workspace_path(workspace, "production.sqlite")
        initial_source = (assets / "login_service.py.txt").read_text(encoding="utf-8")
        source.write_text(initial_source, encoding="utf-8")
        test.write_text((assets / "test_login_service.py.txt").read_text(encoding="utf-8"), encoding="utf-8")
        before = self.run_allowed_command(workspace, self._TEST_COMMAND)
        if before.exit_code == 0 or before.timed_out:
            raise WorkerDecisionError("expected_local_failure_missing")
        observation = {"source_file": "login_service.py", "relevant_source": initial_source[:4000],
                       "local_test": _safe_test_observation(before),
                       "allowed_edit": "password_matches return expression only"}
        decision, usage = self._decide(PATCH_SYSTEM, observation, CodePatchDecision, PATCH_PROMPT_VERSION)
        updated = _replace_password_expression(initial_source, decision.replacement_expression)
        started = monotonic()
        source.write_text(updated, encoding="utf-8")
        self._record(decision.action, decision.file, _safe_expression(decision.replacement_expression),
                     PATCH_PROMPT_VERSION, usage)
        after = self.run_allowed_command(workspace, self._TEST_COMMAND)
        actions = [WorkerAction("propose_code_patch", decision.file, "applied", monotonic() - started),
                   WorkerAction("run_local_tests", "test_login_service.py",
                                "passed" if after.exit_code == 0 else "failed", after.duration_seconds)]
        changed = [ChangedFile("login_service.py", "Applied validated model-selected expression."),
                   ChangedFile("test_login_service.py", "Installed fixed local regression fixture.")]
        if after.exit_code != 0 or after.timed_out:
            execution = WorkerExecutionResult(task, str(workspace),
                WorkerClaim("FAILED", "Local checks failed after model patch.", task.task_id),
                changed, [before, after], actions=actions)
            self._results[task.task_id] = execution
            return execution
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE users (email TEXT PRIMARY KEY, password TEXT NOT NULL)")
            connection.execute("INSERT INTO users VALUES (?, ?)", ("demo@proof.local", "fake-test-password"))
            connection.commit()
        changed.append(ChangedFile("production.sqlite", "Prepared disposable production database without session migration."))
        started = monotonic()
        server = ControlledLoginServer(workspace, database)
        try:
            server.start()
        except Exception:
            server.close()
            raise
        self._servers[task.task_id] = server
        actions.append(WorkerAction("deploy_loopback_application", server.base_url, "running", monotonic() - started))
        execution = WorkerExecutionResult(task, str(workspace),
            WorkerClaim("COMPLETED", "Local tests passed; disposable release running.", task.task_id, "local-v0.6"),
            changed, [before, after], actions=actions)
        self._results[task.task_id] = execution
        return execution

    def repair_task(self, request: RepairRequest) -> WorkerExecutionResult:
        original = self._results.get(request.task_id)
        if original is None or request.task_id not in self._servers:
            raise CommandRejected("Unknown or undeployed task")
        shared = (bool(request.failed_evidence)
                  and request.evidence == tuple(item.evidence_id for item in request.failed_evidence))
        legacy = (request.instructions == "choose_repair_from_failed_proof_evidence"
                  and any(item.condition_id == "C2" and item.operation == "json_value"
                          and item.expected == "True" and item.observed == "False"
                          and item.status == "FAILED" for item in request.failed_evidence))
        scenario = (request.instructions == "choose_repair_from_verified_schema_evidence"
                    and bool(request.source_run_id and request.contract_id)
                    and len(request.failed_evidence) == 1
                    and all(item.evidence_id and item.condition_id
                            and item.operation == "json_value" and item.expected == "True"
                            and item.observed == "False" and item.status == "FAILED"
                            and item.target_ref == "production" and item.route_ref == "schema"
                            and item.json_path == "auth_sessions_exists"
                            and item.source_run_id == request.source_run_id
                            for item in request.failed_evidence))
        if not shared or not (legacy or scenario):
            raise CommandRejected("Repair requires failed production-schema evidence")
        database = self.workspace_path(Path(original.workspace), "production.sqlite")
        if not database.is_file():
            raise WorkspaceViolation("Assigned production database is missing")
        observation = {"failed_evidence": [asdict(item) for item in request.failed_evidence],
                       "available_migrations": ["auth_sessions_v1"],
                       "production_database": "production.sqlite"}
        decision, usage = self._decide(REPAIR_SYSTEM, observation, MigrationDecision, REPAIR_PROMPT_VERSION)
        started = monotonic()
        with closing(sqlite3.connect(database)) as connection:
            users = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='users'").fetchone()
            sessions = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='auth_sessions'").fetchone()
            if not users or sessions:
                raise CommandRejected("Production migration precondition is not met")
            connection.execute(self._MIGRATION)  # fixed trusted SQL; never model-provided
            connection.commit()
        self._record(decision.action, "production.sqlite", decision.migration_ref,
                     REPAIR_PROMPT_VERSION, usage)
        action = WorkerAction("apply_authorized_migration", "production.sqlite", "applied", monotonic() - started)
        check = self.run_allowed_command(Path(original.workspace), self._TEST_COMMAND)
        execution = WorkerExecutionResult(original.task, original.workspace,
            WorkerClaim("COMPLETED" if check.exit_code == 0 else "FAILED",
                        "Authorized migration applied; local tests rerun.", request.task_id, "local-v0.6-repaired"),
            [ChangedFile("production.sqlite", "Applied trusted auth_sessions_v1 migration.")], [check],
            actions=[action, WorkerAction("run_local_tests", "test_login_service.py",
                                         "passed" if check.exit_code == 0 else "failed", check.duration_seconds)])
        self._results[request.task_id] = execution
        return execution
