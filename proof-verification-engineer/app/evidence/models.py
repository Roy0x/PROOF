from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


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


def report_to_dict(report: VerificationReport) -> dict[str, Any]:
    return {"verdict": report.verdict, "passed": report.passed, "failed": report.failed,
            "blocked": report.blocked, "total": report.total,
            "evidence": [asdict(item) for item in report.evidence]}
