"""Opt-in live or default offline-mock AI worker; independent local PROOF verification."""
from __future__ import annotations

import argparse
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from uuid import uuid4

from app.core.intelligence import TrustedContext
from app.core.intelligence_workflow import IntelligenceWorkflow
from app.core.verification_engine import ProofPackV3, VerificationEngine
from app.demo_v05 import GOAL, OfflineFixtureModel
from app.llm.token_factory import ModelClient, ModelResponse, ModelUsage, ReasoningPolicy, TokenFactoryClient
from app.storage.proof_packs_v3 import ProofPackStore
from app.verifiers.browser import BrowserVerifier
from app.verifiers.http import HTTPVerifier
from app.verifiers.registry import VerifierRegistry
from app.verifiers.shell import ShellVerifier
from app.workers.models import FailedEvidenceReference, RepairRequest, WorkerExecutionResult, WorkerTask
from app.workers.nemotron_coding import NemotronCodingWorkerAdapter


class OfflineWorkerModel:
    """Deterministic fake of the worker ModelClient protocol; no network path."""

    model_id = "offline/mock-nemotron-worker"

    def __init__(self, responses: tuple[dict, ...] | None = None) -> None:
        self.responses = responses or (
            {"action": "propose_code_patch", "file": "login_service.py",
             "function": "password_matches", "replacement_expression": "candidate == stored"},
            {"action": "apply_authorized_migration", "migration_ref": "auth_sessions_v1"},
        )
        self.calls: list[dict] = []

    def complete_json(self, system: str, user: str, *, max_output_tokens: int = 800,
                      reasoning_policy: ReasoningPolicy = ReasoningPolicy.PROVIDER_DEFAULT) -> ModelResponse:
        if len(self.calls) >= len(self.responses):
            raise RuntimeError("Offline worker model received an unexpected extra call")
        self.calls.append({"system": system, "user": user, "max_output_tokens": max_output_tokens,
                           "reasoning_policy": reasoning_policy})
        return ModelResponse(self.responses[len(self.calls) - 1],
                             ModelUsage(self.model_id, 0, 0, 0.0, reasoning_tokens=0))


def repair_request_from_proof(pack: ProofPackV3, task_id: str) -> RepairRequest:
    """Extract minimal genuine failed evidence; migration choice is left to the model."""
    if pack.verdict != "FAILED":
        raise ValueError("Repair requires a failed PROOF run")
    failed = tuple(item for item in pack.evidence
                   if item.condition_id == "C2" and item.operation == "json_value"
                   and item.status == "FAILED" and item.expected is True and item.observed is False)
    if not failed:
        raise ValueError("Failed production schema evidence is missing")
    refs = tuple(FailedEvidenceReference(item.evidence_id, item.condition_id, item.operation,
                                         str(item.expected), str(item.observed), "FAILED") for item in failed)
    return RepairRequest(task_id, tuple(item.evidence_id for item in failed),
                         "choose_repair_from_failed_proof_evidence", refs)


@dataclass(frozen=True)
class DemoOutcome:
    first: ProofPackV3
    second: ProofPackV3
    first_path: Path
    second_path: Path
    worker_first: WorkerExecutionResult
    worker_repair: WorkerExecutionResult
    repair_request: RepairRequest
    worker_model_calls: int
    proof_model_calls: int
    model_execution: str
    workspace_path: Path


def run_demo(*, output_root: Path | None = None, worker_model: ModelClient | None = None,
             live: bool = False) -> DemoOutcome:
    if live and os.getenv("PROOF_RUN_LIVE_AI_WORKER") != "1":
        raise ValueError("Live worker requires PROOF_RUN_LIVE_AI_WORKER=1")
    if live and worker_model is not None:
        raise ValueError("Live worker cannot be overridden with an injected model")
    model = TokenFactoryClient(retries=0) if live else (worker_model or OfflineWorkerModel())
    execution_mode = "live_nebius" if live else "offline_mock"
    temp_root = Path(tempfile.gettempdir()).resolve()
    packs_root = (output_root if output_root is not None else
                  Path(tempfile.mkdtemp(prefix="proof-v06-packs-"))).resolve()
    if packs_root == temp_root or not packs_root.is_relative_to(temp_root):
        raise ValueError("Proof Packs must be under a disposable temporary root")
    store = ProofPackStore(packs_root)
    browser = BrowserVerifier()
    run_prefix = uuid4().hex
    first_run, second_run = f"{run_prefix}_1", f"{run_prefix}_2"
    with tempfile.TemporaryDirectory(prefix="proof-v06-task-") as task_root:
        worker = NemotronCodingWorkerAdapter(Path(task_root) / "workspaces", model)
        try:
            task = WorkerTask(GOAL)
            worker_first = worker.run_task(task)
            if worker_first.claim.status != "COMPLETED" or worker_first.command_results[-1].exit_code != 0:
                raise RuntimeError("Model-selected patch did not pass local tests")
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
            proof_model = OfflineFixtureModel()
            workflow = IntelligenceWorkflow(proof_model, context)
            prepared = workflow.prepare(GOAL)
            engine = VerificationEngine(VerifierRegistry({
                "http": HTTPVerifier(), "browser": browser, "shell": ShellVerifier()}))
            artifact_root = str(packs_root / "artifacts")
            first = workflow.verify(prepared, engine, first_run, artifact_root=artifact_root)
            first.intelligence.update({
                "model_execution": execution_mode,
                "proof_planning_execution": "offline_fixed_responses",
                "worker_claim": asdict(worker_first.claim),
                "worker_actions": [asdict(action) for action in worker_first.actions],
                "worker_model_decisions": [asdict(record) for record in worker.decision_records],
            })
            first_path = store.save(first)
            first_bytes = first_path.read_bytes()
            browser.close(first_run)
            if first.verdict != "FAILED":
                raise RuntimeError("Production defect was not independently detected")
            request = repair_request_from_proof(first, task.task_id)
            worker_repair = worker.repair_task(request)
            if worker_repair.claim.status != "COMPLETED" or worker_repair.command_results[-1].exit_code != 0:
                raise RuntimeError("Model-selected repair did not complete")
            second = workflow.verify(prepared, engine, second_run, artifact_root=artifact_root)
            second.intelligence.update({
                "model_execution": execution_mode,
                "proof_planning_execution": "offline_fixed_responses",
                "worker_claim": asdict(worker_first.claim),
                "worker_model_decisions": [asdict(record) for record in worker.decision_records],
                "repair_request": asdict(request),
                "repair_actions": [asdict(action) for action in worker_repair.actions],
            })
            second_path = store.save(second)
            browser.close(second_run)
            if first_path.read_bytes() != first_bytes:
                raise RuntimeError("First Proof Pack changed during repair")
            if second.verdict != "VERIFIED":
                raise RuntimeError("Independent reverification did not verify deployment")
            return DemoOutcome(first, second, first_path, second_path, worker_first, worker_repair,
                               request, worker.model_calls, proof_model.calls, execution_mode,
                               Path(worker_first.workspace))
        finally:
            browser.close(first_run)
            browser.close(second_run)
            worker.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run local PROOF v0.6 AI worker loop")
    parser.add_argument("--output", type=Path, help="Temporary-directory root for durable Proof Packs")
    parser.add_argument("--live", action="store_true", help="Requires PROOF_RUN_LIVE_AI_WORKER=1; two Nebius decisions")
    args = parser.parse_args()
    result = run_demo(output_root=args.output, live=args.live)
    print(f"WORKER MODEL: {result.second.intelligence['worker_model_decisions'][0]['model_id']} ({result.model_execution})")
    print(f"AI CODE ACTION: {result.worker_first.actions[0].outcome}")
    print(f"LOCAL TESTS: {'PASSED' if result.worker_first.command_results[-1].exit_code == 0 else 'FAILED'}")
    print(f"WORKER CLAIM: {result.worker_first.claim.status}")
    print(f"PROOF RUN 1: {result.first.verdict}")
    defect = next(item for item in result.first.evidence if item.operation == "json_value" and item.status == "FAILED")
    print(f"PRODUCTION DEFECT: {defect.operation} expected={defect.expected} observed={defect.observed}")
    print(f"AI REPAIR DECISION: {result.second.intelligence['worker_model_decisions'][-1]['selected_value']}")
    print(f"REPAIR EXECUTION: {result.worker_repair.actions[0].outcome}")
    print(f"PROOF RUN 2: {result.second.verdict}")
    print(f"CONTRACT: {'unchanged' if result.first.contract_id == result.second.contract_id and result.first.contract_version == result.second.contract_version else 'changed'}")
    print(f"PROOF PACKS: {result.first_path} | {result.second_path}")


if __name__ == "__main__":
    main()
