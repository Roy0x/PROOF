from app.core.verification_engine import VerificationEngine
from app.evidence.models import VerificationEvidence
from app.verifiers.models import PlannedCondition, VerificationContext, VerificationPlan, VerificationStep
from app.verifiers.registry import VerifierRegistry

class FakeVerifier:
    def __init__(self, status): self.status = status
    def verify(self, condition_id, step, context):
        return VerificationEvidence(condition_id, "http", step.operation, step.expected, "observed", self.status, context.run_id)

def plan(*conditions): return VerificationPlan("contract_123", 1, conditions)

def test_worker_claim_cannot_override_independent_failed_evidence():
    condition = PlannedCondition("health", "health", True, (VerificationStep("http", "status", 200),))
    pack = VerificationEngine(VerifierRegistry({"http": FakeVerifier("FAILED")})).verify("goal", plan(condition), VerificationContext("run_1"))
    assert pack.verdict == "FAILED" and pack.contract_id == "contract_123"

def test_non_critical_failure_remains_visible_without_failing_proof():
    good = PlannedCondition("good", "good", True, (VerificationStep("http", "status"),))
    optional = PlannedCondition("optional", "optional", False, (VerificationStep("http", "status"),))
    registry = VerifierRegistry({"http": FakeVerifier("VERIFIED")})
    pack = VerificationEngine(registry).verify("goal", plan(good, optional), VerificationContext("run_2"))
    assert pack.verdict == "VERIFIED"

def test_unknown_verifier_fails_closed_and_dependency_blocks():
    unknown = PlannedCondition("auth", "auth", True, (VerificationStep("browser", "navigate"),))
    dependent = PlannedCondition("dashboard", "dash", True, (VerificationStep("http", "status"),), ("auth",))
    pack = VerificationEngine(VerifierRegistry({})).verify("goal", plan(unknown, dependent), VerificationContext("run_3"))
    assert pack.verdict == "BLOCKED" and pack.condition_results[1].blocked_by == "auth"
