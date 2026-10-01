from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any
from uuid import uuid4


@dataclass
class Evidence:
    condition_id: str
    condition_name: str
    verdict: str
    method: str
    expected: str
    observed: str
    details: dict[str, Any]
    timestamp: str


@dataclass
class VerificationReport:
    verdict: str
    passed: int
    failed: int
    blocked: int
    total: int
    evidence: list[Evidence]


@dataclass(frozen=True)
class VerificationEvidence:
    condition_id: str
    verifier_type: str
    operation: str
    expected: Any
    observed: Any
    status: str
    run_id: str
    evidence_id: str = ""
    timestamp: str = ""
    duration_seconds: float = 0.0
    error: str | None = None
    artifacts: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.evidence_id:
            object.__setattr__(self, "evidence_id", uuid4().hex)


def report_to_dict(report: VerificationReport) -> dict[str, Any]:
    return {"verdict": report.verdict, "passed": report.passed, "failed": report.failed,
            "blocked": report.blocked, "total": report.total,
            "evidence": [asdict(item) for item in report.evidence]}
