"""Offline, controlled worker -> evidence -> migration -> reverification demo.

Run with: .venv/Scripts/python.exe -m app.demo_v05
No provider request is made; the compiler/planner client returns fixed local fixtures.
"""
from __future__ import annotations

import argparse
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from app.core.intelligence import TrustedContext
from app.core.intelligence_workflow import IntelligenceWorkflow
from app.core.verification_engine import ProofPackV3, VerificationEngine
from app.llm.token_factory import ModelResponse, ModelUsage, ReasoningPolicy
from app.storage.proof_packs_v3 import ProofPackStore
from app.verifiers.browser import BrowserVerifier
from app.verifiers.http import HTTPVerifier
from app.verifiers.registry import VerifierRegistry
from app.verifiers.shell import ShellVerifier
from app.workers.models import (FailedEvidenceReference, RepairRequest,
                                WorkerExecutionResult, WorkerTask)
from app.workers.sqlite_login import SqliteLoginWorkerAdapter


GOAL = "Fix the login bug and deploy the application."
_CONTRACT = {"conditions": [
    {"id": "C1", "description": "Production home page is reachable", "critical": True, "prerequisites": []},
    {"id": "C2", "description": "Production login authenticates the valid test user", "critical": True, "prerequisites": []},
    {"id": "C3", "description": "Authenticated dashboard is visible", "critical": True, "prerequisites": ["C2"]},
    {"id": "C4", "description": "Local regression tests pass", "critical": True, "prerequisites": []},
]}
_STEPS = (
    {"steps": [{"verifier": "http", "operation": "status", "target_ref": "production",
                "route_ref": "home", "expected": 200}]},
    {"steps": [
        {"verifier": "browser", "operation": "navigate", "target_ref": "production", "route_ref": "login"},
        {"verifier": "browser", "operation": "fill", "target_ref": "production",
         "selector_ref": "password", "credential_ref": "valid_test_user.password"},
        {"verifier": "browser", "operation": "click", "target_ref": "production", "selector_ref": "submit"},
        {"verifier": "browser", "operation": "wait_for", "target_ref": "production", "selector_ref": "dashboard_heading"},
        {"verifier": "http", "operation": "json_value", "target_ref": "production",
         "route_ref": "schema", "json_path": "auth_sessions_exists", "expected": True},
    ]},
    {"steps": [{"verifier": "browser", "operation": "assert_text", "target_ref": "production",
                "selector_ref": "dashboard_heading", "expected": "Deployment dashboard"}]},
    {"steps": [{"verifier": "shell", "operation": "pytest", "command_ref": "regression"}]},
)


class OfflineFixtureModel:
    """Fixed responses exercise v0.4 parsing/validation without impersonating live inference."""

    model_id = "offline/controlled-fixture"

    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, system: str, user: str, *, max_output_tokens: int = 800,
                      reasoning_policy: ReasoningPolicy = ReasoningPolicy.PROVIDER_DEFAULT) -> ModelResponse:
        if self.calls > len(_STEPS):
            raise RuntimeError("Offline fixture model received an unexpected extra call")
        data = _CONTRACT if self.calls == 0 else _STEPS[self.calls - 1]
        self.calls += 1
        return ModelResponse(data, ModelUsage(self.model_id, 0, 0, 0.0, reasoning_tokens=0))


def repair_request_from_failed_pack(pack: ProofPackV3, task_id: str) -> RepairRequest:
    if pack.verdict != "FAILED":
        raise ValueError("Repair requires a failed PROOF run")
    failed = tuple(item for item in pack.evidence
                   if item.condition_id == "C2" and item.operation == "json_value"
                   and item.status == "FAILED" and item.expected is True and item.observed is False)
    if not failed:
        raise ValueError("Failed production-login schema evidence is missing")
    refs = tuple(FailedEvidenceReference(item.evidence_id, item.condition_id,
                                         item.operation, str(item.expected), str(item.observed), "FAILED")
                 for item in failed)
    return RepairRequest(task_id, tuple(item.evidence_id for item in failed),
                         "apply_auth_sessions_migration", refs)


@dataclass(frozen=True)
class DemoOutcome:
    first: ProofPackV3
    second: ProofPackV3
    first_path: Path
    second_path: Path
    worker_first: WorkerExecutionResult
    worker_repair: WorkerExecutionResult | None
    repair_request: RepairRequest
    model_calls: int
    workspace_path: Path


def run_demo(*, output_root: Path | None = None, perform_repair: bool = True) -> DemoOutcome:
    temp_root = Path(tempfile.gettempdir()).resolve()
    packs_root = (output_root if output_root is not None else
                  Path(tempfile.mkdtemp(prefix="proof-v05-packs-"))).resolve()
    if packs_root == temp_root or not packs_root.is_relative_to(temp_root):
        raise ValueError("Proof Packs must be written under a disposable temporary root")
    store = ProofPackStore(packs_root)
    browser = BrowserVerifier()
    run_prefix = uuid4().hex
    first_run, second_run = f"{run_prefix}_1", f"{run_prefix}_2"
    with tempfile.TemporaryDirectory(prefix="proof-v05-task-") as task_root:
        worker = SqliteLoginWorkerAdapter(Path(task_root) / "workspaces")
        try:
            task = WorkerTask(GOAL)
            worker_first = worker.run_task(task)
            if worker_first.claim.status != "COMPLETED" or worker_first.command_results[0].exit_code != 0:
                raise RuntimeError("Controlled worker local checks failed")
            context = TrustedContext(
                production_base_url=worker.production_url(task.task_id),
                workspace=worker_first.workspace,
                routes={"home": "/", "login": "/login", "dashboard": "/dashboard",
                        "schema": "/health/schema"},
                selectors={"password": "#password", "submit": "#submit",
                           "dashboard_heading": "#dashboard-heading"},
                commands={"regression": ("pytest", ("-q",))},
                credentials={"valid_test_user.password": "fake-test-password"},
            )
            model = OfflineFixtureModel()
            workflow = IntelligenceWorkflow(model, context)
            prepared = workflow.prepare(GOAL)
            engine = VerificationEngine(VerifierRegistry({
                "http": HTTPVerifier(), "browser": browser, "shell": ShellVerifier()}))
            artifact_root = str(packs_root / "artifacts")
            first = workflow.verify(prepared, engine, first_run, artifact_root=artifact_root)
            first.intelligence["model_execution"] = "offline_fixed_responses"
            first.intelligence["worker_claim"] = asdict(worker_first.claim)
            first.intelligence["worker_actions"] = [asdict(item) for item in worker_first.actions]
            first_path = store.save(first)
            first_pack_bytes = first_path.read_bytes()
            browser.close(first_run)
            if first.verdict != "FAILED":
                raise RuntimeError("Controlled production defect was not detected")
            request = repair_request_from_failed_pack(first, task.task_id)
            worker_repair = None
            if perform_repair:
                worker_repair = worker.repair_task(request)
                if worker_repair.claim.status != "COMPLETED" or worker_repair.command_results[0].exit_code != 0:
                    raise RuntimeError("Controlled repair did not complete")
            second = workflow.verify(prepared, engine, second_run, artifact_root=artifact_root)
            second.intelligence["model_execution"] = "offline_fixed_responses"
            second.intelligence["worker_claim"] = asdict(worker_first.claim)
            if worker_repair:
                second.intelligence["repair_request"] = asdict(request)
                second.intelligence["repair_actions"] = [asdict(item) for item in worker_repair.actions]
            second_path = store.save(second)
            if first_path.read_bytes() != first_pack_bytes:
                raise RuntimeError("First failed Proof Pack changed during reverification")
            browser.close(second_run)
            if perform_repair and second.verdict != "VERIFIED":
                raise RuntimeError("Reverification did not verify the repaired deployment")
            return DemoOutcome(first, second, first_path, second_path, worker_first,
                               worker_repair, request, model.calls, Path(worker_first.workspace))
        finally:
            browser.close(first_run)
            browser.close(second_run)
            worker.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the offline PROOF v0.5 controlled repair loop")
    parser.add_argument("--output", type=Path, help="Temporary-directory location for durable Proof Packs")
    args = parser.parse_args()
    result = run_demo(output_root=args.output)
    failed = next(item.description for item in result.first.condition_results if item.status == "FAILED")
    repair = result.worker_repair.actions[0].outcome if result.worker_repair else "not applied"
    print(f"WORKER CLAIM: {result.worker_first.claim.status}")
    print(f"PROOF RUN 1: {result.first.verdict}")
    print(f"FAILED CONDITION: {failed}")
    print(f"REPAIR: migration {repair}")
    print(f"PROOF RUN 2: {result.second.verdict}")
    print(f"PROOF PACKS: {result.first_path} | {result.second_path}")


if __name__ == "__main__":
    main()
