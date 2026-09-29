from __future__ import annotations

from .models import Evidence, VerificationReport
from app.core.contracts import OutcomeContract


def build_report(contract: OutcomeContract, evidence: list[Evidence]) -> VerificationReport:
    passed = sum(item.verdict == "PASS" for item in evidence)
    failed = sum(item.verdict == "FAIL" for item in evidence)
    blocked = sum(item.verdict == "BLOCKED" for item in evidence)
    critical = {condition.id: condition.critical for condition in contract.conditions}
    has_critical_problem = any(item.verdict != "PASS" and critical.get(item.condition_id, True) for item in evidence)
    verdict = "VERIFIED" if not has_critical_problem else ("FAILED" if failed else "BLOCKED")
    return VerificationReport(verdict, passed, failed, blocked, len(evidence), evidence)
