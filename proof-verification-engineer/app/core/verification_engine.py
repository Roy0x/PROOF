from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic
from uuid import uuid4
from app.evidence.models import VerificationEvidence
from app.verifiers.models import VerificationContext, VerificationPlan
from app.verifiers.registry import VerifierRegistry

@dataclass
class ConditionResult:
    condition_id: str; description: str; critical: bool; status: str; evidence_ids: list[str]; blocked_by: str | None = None
@dataclass
class ProofPackV3:
    run_id: str; contract_id: str; contract_version: int; goal: str; verdict: str; condition_results: list[ConditionResult]; evidence: list[VerificationEvidence]; started_at: str; completed_at: str; duration_seconds: float

class VerificationEngine:
    def __init__(self, registry: VerifierRegistry) -> None: self.registry = registry
    def verify(self, goal: str, plan: VerificationPlan, context: VerificationContext) -> ProofPackV3:
        started = monotonic(); evidence=[]; results=[]; prior={}
        for condition in plan.conditions:
            bad = next((x for x in condition.prerequisites if prior.get(x) != "VERIFIED"), None)
            if bad:
                item=VerificationEvidence(condition.condition_id,"engine","prerequisite", "VERIFIED", prior.get(bad), "BLOCKED", context.run_id, error=f"Prerequisite {bad} not verified"); evidence.append(item); status="BLOCKED"
            else:
                items=[]
                for step in condition.steps:
                    verifier=self.registry.get(step.verifier)
                    items.append(verifier.verify(condition.condition_id,step,context) if verifier else VerificationEvidence(condition.condition_id,step.verifier,step.operation,step.expected,None,"BLOCKED",context.run_id,error="Unknown verifier"))
                evidence.extend(items); status=next((s for s in ("FAILED","BLOCKED","INCONCLUSIVE") if any(i.status==s for i in items)), "VERIFIED")
            prior[condition.condition_id]=status; results.append(ConditionResult(condition.condition_id,condition.description,condition.critical,status,[e.evidence_id for e in evidence if e.condition_id==condition.condition_id],bad))
        critical=[r.status for r in results if r.critical]; verdict=next((s for s in ("FAILED","BLOCKED","INCONCLUSIVE") if s in critical), "VERIFIED")
        return ProofPackV3(context.run_id,plan.contract_id,plan.contract_version,goal,verdict,results,evidence,datetime.now(timezone.utc).isoformat(),datetime.now(timezone.utc).isoformat(),monotonic()-started)
