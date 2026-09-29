from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Condition:
    id: str
    name: str
    description: str
    critical: bool
    method: str
    params: dict[str, Any]


@dataclass
class OutcomeContract:
    goal: str
    summary: str
    conditions: list[Condition]
    generated_by: str = "deterministic-fallback"


def contract_to_dict(contract: OutcomeContract) -> dict[str, Any]:
    return {
        "goal": contract.goal,
        "summary": contract.summary,
        "generated_by": contract.generated_by,
        "conditions": [asdict(condition) for condition in contract.conditions],
    }
