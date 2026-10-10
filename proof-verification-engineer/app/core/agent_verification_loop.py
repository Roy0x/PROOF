"""Scenario-specific v0.7 loop: model proposals, trusted actions, independent evidence."""
from __future__ import annotations

import json
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urljoin
from uuid import uuid4

from app.core.intelligence import FrozenOutcomeContract, PlanningError, TrustedContext
from app.core.intelligence_workflow import IntelligenceWorkflow, PreparedVerification
from app.core.verification_engine import ProofPackV3, VerificationEngine
from app.demo_v05 import GOAL
from app.llm.intelligence import StepProposal, VerificationPlanner
from app.llm.token_factory import ModelClient, ModelError, ModelResponse, ModelUsage, ReasoningPolicy
from app.storage.proof_packs_v3 import ProofPackStore
from app.verifiers.browser import BrowserVerifier
from app.verifiers.http import HTTPVerifier
from app.verifiers.registry import VerifierRegistry
from app.verifiers.shell import ShellVerifier
from app.workers.models import FailedEvidenceReference, RepairRequest, WorkerExecutionResult, WorkerTask
from app.workers.nemotron_coding import NemotronCodingWorkerAdapter


MAX_CONDITIONS = 5
MAX_MODEL_REQUESTS = 8
ACCEPTANCE = (
    {"ref": "home", "outcome": "Deployed public home page responds successfully"},
    {"ref": "auth", "outcome": "Valid test user authenticates in production"},
    {"ref": "dashboard", "outcome": "Authenticated user reaches the dashboard"},
    {"ref": "schema", "outcome": "Production session schema exists"},
    {"ref": "regression", "outcome": "Local regression tests pass"},
)
REQUIRED_STEPS: dict[str, tuple[dict, ...]] = {
    "home": ({"verifier": "http", "operation": "status", "target_ref": "production",
              "route_ref": "home", "expected": 200},),
    "auth": (
        {"verifier": "browser", "operation": "navigate", "target_ref": "production", "route_ref": "login"},
        {"verifier": "browser", "operation": "fill", "target_ref": "production",
         "selector_ref": "password", "credential_ref": "valid_test_user.password"},
        {"verifier": "browser", "operation": "click", "target_ref": "production", "selector_ref": "submit"},
        {"verifier": "browser", "operation": "wait_for", "target_ref": "production",
         "selector_ref": "dashboard_heading"},
    ),
    "dashboard": ({"verifier": "browser", "operation": "assert_text", "target_ref": "production",
                   "selector_ref": "dashboard_heading", "expected": "Deployment dashboard"},),
    "schema": ({"verifier": "http", "operation": "json_value", "target_ref": "production",
                "route_ref": "schema", "json_path": "auth_sessions_exists", "expected": True},),
    "regression": ({"verifier": "shell", "operation": "pytest", "command_ref": "regression"},),
}
REQUIRED_CHECKS = {ref: {"steps": steps} for ref, steps in REQUIRED_STEPS.items()}


def scenario_context(worker: NemotronCodingWorkerAdapter, task_id: str, workspace: str) -> TrustedContext:
    return TrustedContext(
        production_base_url=worker.production_url(task_id), workspace=workspace,
        routes={"home": "/", "login": "/login", "dashboard": "/dashboard", "schema": "/health/schema"},
        selectors={"password": "#password", "submit": "#submit", "dashboard_heading": "#dashboard-heading"},
        commands={"regression": ("pytest", ("-q",))},
        credentials={"valid_test_user.password": "fake-test-password"},
    )


def validate_scenario_contract(contract: FrozenOutcomeContract) -> None:
    if len(contract.conditions) > MAX_CONDITIONS:
        raise PlanningError("compilation", "excessive_conditions", "Scenario allows at most five conditions")
    refs = [item.acceptance_ref for item in contract.conditions]
    if len(refs) != len(ACCEPTANCE) or set(refs) != {item["ref"] for item in ACCEPTANCE}:
        raise PlanningError("compilation", "insufficient_contract", "Contract must cover each trusted acceptance reference exactly once")
    if not all(item.critical for item in contract.conditions):
        raise PlanningError("compilation", "noncritical_outcome", "All scenario outcomes must be critical")
    by_ref = {item.acceptance_ref: item for item in contract.conditions}
    unsafe = [item for ref in ("home", "auth", "schema", "regression")
              if (item := by_ref[ref]).prerequisites]
    if unsafe:
        rejected = [{"condition_id": item.id, "acceptance_ref": item.acceptance_ref,
                     "rejected_prerequisite_ids": item.prerequisites} for item in unsafe]
        raise PlanningError("compilation", "unsafe_prerequisite",
                            f"Independent checks cannot be blocked by prerequisites: {json.dumps(rejected)}")
    if by_ref["dashboard"].prerequisites != (by_ref["auth"].id,):
        raise PlanningError("compilation", "invalid_dependency", "Dashboard must depend on production authentication")


def validate_scenario_plan(prepared: PreparedVerification, context: TrustedContext) -> None:
    validate_scenario_contract(prepared.contract)
    if (prepared.plan.contract_id != prepared.contract.contract_id
            or prepared.plan.contract_version != prepared.contract.version
            or [item.condition_id for item in prepared.plan.conditions]
            != [item.id for item in prepared.contract.conditions]):
        raise PlanningError("planning", "contract_mismatch", "Plan differs from frozen contract")
    manifest = context.manifest()
    for condition, planned in zip(prepared.contract.conditions, prepared.plan.conditions):
        expected = tuple(VerificationPlanner._validate_step(StepProposal.model_validate(step), manifest, context)
                         for step in REQUIRED_STEPS[condition.acceptance_ref])
        if (planned.description != condition.description or planned.critical != condition.critical
                or planned.prerequisites != condition.prerequisites or planned.steps != expected):
            raise PlanningError("planning", "insufficient_coverage",
                                f"Plan lacks exact trusted check for {condition.acceptance_ref}")


class RequestBudgetClient:
    """One central model-call counter; stores only usage, never prompts or responses."""

    def __init__(self, client: ModelClient, limit: int = MAX_MODEL_REQUESTS,
                 *, provider_enabled: bool = False) -> None:
        self._client = client
        self.model_id = client.model_id
        self.limit = limit
        self.provider_enabled = provider_enabled
        self.attempted = 0
        self.completed: list[ModelUsage] = []

    def complete_json(self, system: str, user: str, *, max_output_tokens: int = 800,
                      reasoning_policy: ReasoningPolicy = ReasoningPolicy.PROVIDER_DEFAULT) -> ModelResponse:
        if self.attempted >= self.limit:
            raise ModelError("request_budget_exhausted", "Model request budget exhausted")
        self.attempted += 1
        response = self._client.complete_json(system, user, max_output_tokens=max_output_tokens,
                                              reasoning_policy=reasoning_policy)
        if response.usage.request_count != 1:
            raise ModelError("unexpected_retry", "Model client made more than one request")
        self.completed.append(response.usage)
        return response

    def usage_summary(self) -> dict:
        def total(field: str):
            values = [getattr(item, field) for item in self.completed]
            return sum(values) if values and all(value is not None for value in values) else None
        return {"attempted_requests": self.attempted, "completed_requests": len(self.completed),
                "provider_requests_attempted": self.attempted if self.provider_enabled else 0,
                "provider_request_count": (sum(item.request_count for item in self.completed)
                                           if self.provider_enabled else 0),
                "prompt_tokens": total("prompt_tokens"),
                "completion_tokens": total("completion_tokens"),
                "reasoning_tokens": total("reasoning_tokens")}


def repair_request_from_evidence(pack: ProofPackV3, prepared: PreparedVerification,
                                 context: TrustedContext, store: ProofPackStore,
                                 expected_run_id: str, task_id: str, fingerprint: str) -> RepairRequest:
    """Bind one failed schema probe to the persisted current run and trusted plan."""
    if (pack.verdict != "FAILED" or pack.run_id != expected_run_id
            or pack.contract_id != prepared.contract.contract_id
            or pack.contract_version != prepared.contract.version
            or pack.intelligence.get("plan_fingerprint") != fingerprint):
        raise PlanningError("repair", "stale_or_invalid_pack", "Repair requires the current failed frozen-plan pack")
    persisted = store.load(pack.run_id)
    if persisted != json.loads(json.dumps(asdict(pack), default=str)):
        raise PlanningError("repair", "pack_mismatch", "Persisted evidence differs from supplied Proof Pack")
    schema = next(item for item in prepared.contract.conditions if item.acceptance_ref == "schema")
    plan_condition = next(item for item in prepared.plan.conditions if item.condition_id == schema.id)
    expected_step = VerificationPlanner._validate_step(
        StepProposal.model_validate(REQUIRED_STEPS["schema"][0]), context.manifest(), context)
    if plan_condition.steps != (expected_step,) or schema.prerequisites:
        raise PlanningError("repair", "missing_schema_probe", "Trusted production schema probe is absent")
    condition_result = next((item for item in pack.condition_results if item.condition_id == schema.id), None)
    expected_url = urljoin(context.production_base_url + "/", context.routes["schema"].lstrip("/"))
    if condition_result is None or condition_result.status != "FAILED":
        raise PlanningError("repair", "missing_failed_condition", "Schema condition did not fail")
    candidates = [item for item in pack.evidence if item.condition_id == schema.id
                  and item.evidence_id in condition_result.evidence_ids
                  and item.run_id == expected_run_id and item.verifier_type == "http"
                  and item.operation == "json_value" and item.status == "FAILED"
                  and item.expected is True and item.observed is False
                  and item.metadata.get("url") == expected_url
                  and item.metadata.get("method") == "GET"
                  and item.metadata.get("status_code") == 200]
    if len(candidates) != 1:
        raise PlanningError("repair", "missing_schema_evidence", "Exactly one failed trusted schema observation is required")
    item = candidates[0]
    reference = FailedEvidenceReference(item.evidence_id, schema.id, item.operation,
        "True", "False", "FAILED", target_ref="production", route_ref="schema",
        json_path="auth_sessions_exists", source_run_id=pack.run_id)
    return RepairRequest(task_id, (item.evidence_id,), "choose_repair_from_verified_schema_evidence",
                         (reference,), source_run_id=pack.run_id, contract_id=pack.contract_id)


@dataclass(frozen=True)
class LoopOutcome:
    first: ProofPackV3
    second: ProofPackV3
    first_path: Path
    second_path: Path
    prepared: PreparedVerification
    plan_fingerprint: str
    worker_first: WorkerExecutionResult
    worker_repair: WorkerExecutionResult
    repair_request: RepairRequest
    usage: dict
    execution_mode: str
    workspace_path: Path


class AgentVerificationLoop:
    def __init__(self, client: ModelClient, execution_mode: str, output_root: Path | None = None) -> None:
        if execution_mode not in ("offline_mock", "live_nebius"):
            raise ValueError("Unknown model execution mode")
        self.client = RequestBudgetClient(client, provider_enabled=execution_mode == "live_nebius")
        self.execution_mode = execution_mode
        temporary = Path(tempfile.gettempdir()).resolve()
        root = (output_root or Path(tempfile.mkdtemp(prefix="proof-v07-packs-"))).resolve()
        if root == temporary or not root.is_relative_to(temporary):
            raise ValueError("Proof Packs must be under a disposable temporary root")
        self.store = ProofPackStore(root)

    def run(self, goal: str = GOAL) -> LoopOutcome:
        browser = BrowserVerifier()
        prefix = uuid4().hex
        first_run, second_run = f"{prefix}_1", f"{prefix}_2"
        with tempfile.TemporaryDirectory(prefix="proof-v07-task-") as temporary:
            worker = NemotronCodingWorkerAdapter(Path(temporary) / "workspaces", self.client)
            try:
                task = WorkerTask(goal)
                first_worker = worker.run_task(task)
                if first_worker.claim.status != "COMPLETED" or first_worker.command_results[-1].exit_code != 0:
                    raise RuntimeError("Model-selected patch did not pass real local tests")
                context = scenario_context(worker, task.task_id, first_worker.workspace)
                workflow = IntelligenceWorkflow(self.client, context)
                prepared = workflow.prepare(goal, trusted_requirements=ACCEPTANCE,
                    required_checks=REQUIRED_CHECKS, contract_guard=validate_scenario_contract,
                    plan_guard=lambda item: validate_scenario_plan(item, context))
                fingerprint = workflow._fingerprint(prepared)
                engine = VerificationEngine(VerifierRegistry({
                    "http": HTTPVerifier(), "browser": browser, "shell": ShellVerifier()}))
                artifact_root = str(self.store.root / "artifacts")
                first = workflow.verify(prepared, engine, first_run, artifact_root=artifact_root)
                first.intelligence.update({
                    "model_execution": self.execution_mode,
                    "plan_fingerprint": fingerprint,
                    "worker_claim": asdict(first_worker.claim),
                    "worker_actions": [asdict(item) for item in first_worker.actions],
                    "worker_model_decisions": [asdict(item) for item in worker.decision_records],
                    "failed_evidence_ids": [item.evidence_id for item in first.evidence if item.status == "FAILED"],
                    "model_usage": self.client.usage_summary(),
                })
                first_path = self.store.save(first)
                first_bytes = first_path.read_bytes()
                browser.close(first_run)
                if first.verdict != "FAILED":
                    raise RuntimeError("Production defect was not independently detected")
                request = repair_request_from_evidence(first, prepared, context, self.store,
                                                       first_run, task.task_id, fingerprint)
                repaired = worker.repair_task(request)
                if repaired.claim.status != "COMPLETED" or repaired.command_results[-1].exit_code != 0:
                    raise RuntimeError("Model-selected repair did not pass local checks")
                second = workflow.verify(prepared, engine, second_run, artifact_root=artifact_root)
                second.intelligence.update({
                    "model_execution": self.execution_mode,
                    "plan_fingerprint": fingerprint,
                    "worker_claim": asdict(first_worker.claim),
                    "worker_model_decisions": [asdict(item) for item in worker.decision_records],
                    "failed_evidence_ids": first.intelligence["failed_evidence_ids"],
                    "repair_request": asdict(request),
                    "repair_actions": [asdict(item) for item in repaired.actions],
                    "model_usage": self.client.usage_summary(),
                })
                second_path = self.store.save(second)
                browser.close(second_run)
                if first_path.read_bytes() != first_bytes:
                    raise RuntimeError("First Proof Pack changed during reverification")
                if second.verdict != "VERIFIED":
                    raise RuntimeError("Independent reverification did not verify deployment")
                return LoopOutcome(first, second, first_path, second_path, prepared, fingerprint,
                                   first_worker, repaired, request, self.client.usage_summary(),
                                   self.execution_mode, Path(first_worker.workspace))
            finally:
                browser.close(first_run)
                browser.close(second_run)
                worker.close()
