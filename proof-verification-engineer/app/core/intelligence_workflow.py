"""Prepare once, verify repeatedly with independent v0.3 evidence."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from urllib.parse import urlsplit

from app.core.intelligence import FrozenOutcomeContract, PlanningError, TrustedContext
from app.core.verification_engine import ProofPackV3, VerificationEngine
from app.llm.intelligence import OutcomeContractCompiler, VerificationPlanner
from app.llm.token_factory import ModelClient
from app.verifiers.models import VerificationContext, VerificationPlan


@dataclass(frozen=True)
class PreparedVerification:
    contract: FrozenOutcomeContract
    plan: VerificationPlan


class IntelligenceWorkflow:
    def __init__(self, client: ModelClient, trusted_context: TrustedContext) -> None:
        self._client = client
        self._context = trusted_context
        self._prepared_hashes: dict[str, str] = {}

    @staticmethod
    def _fingerprint(prepared: PreparedVerification) -> str:
        content = {"contract": prepared.contract.model_dump(mode="json"), "plan": asdict(prepared.plan)}
        return sha256(json.dumps(content, sort_keys=True, default=str).encode()).hexdigest()

    def prepare(self, goal: str) -> PreparedVerification:
        contract = OutcomeContractCompiler(self._client).compile(goal, self._context)
        plan = VerificationPlanner(self._client).plan(contract, self._context.manifest(), self._context)
        prepared = PreparedVerification(contract, plan)
        self._prepared_hashes[contract.contract_id] = self._fingerprint(prepared)
        return prepared

    def verify(self, prepared: PreparedVerification, engine: VerificationEngine, run_id: str,
               *, artifact_root: str = "artifacts/verification") -> ProofPackV3:
        if self._prepared_hashes.get(prepared.contract.contract_id) != self._fingerprint(prepared):
            raise PlanningError("verification", "plan_changed", "Prepared contract or plan changed after validation")
        if prepared.contract.contract_id != prepared.plan.contract_id or prepared.contract.version != prepared.plan.contract_version:
            raise PlanningError("verification", "contract_mismatch", "Plan does not match frozen contract")
        if [(c.id, c.description, c.critical, c.prerequisites) for c in prepared.contract.conditions] != [
            (c.condition_id, c.description, c.critical, c.prerequisites) for c in prepared.plan.conditions]:
            raise PlanningError("verification", "contract_mismatch", "Plan conditions differ from frozen contract")
        context = VerificationContext(run_id, self._context.production_base_url, self._context.workspace,
                                      artifact_root, credentials=self._context.credentials,
                                      allowed_origin=f"{urlsplit(self._context.production_base_url).scheme}://{urlsplit(self._context.production_base_url).netloc}")
        metadata = {"contract": prepared.contract.model_dump(mode="json"),
                    "plan_provenance": prepared.plan.provenance,
                    "plan": asdict(prepared.plan)}
        return engine.verify(prepared.contract.goal, prepared.plan, context, intelligence=metadata)
