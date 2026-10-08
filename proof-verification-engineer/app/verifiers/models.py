from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Literal

@dataclass(frozen=True)
class VerificationStep:
    verifier: Literal["http", "shell", "browser"]
    operation: str
    expected: Any = None
    target: str | None = None
    params: dict[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class PlannedCondition:
    condition_id: str
    description: str
    critical: bool
    steps: tuple[VerificationStep, ...]
    prerequisites: tuple[str, ...] = ()

@dataclass(frozen=True)
class VerificationPlan:
    contract_id: str
    contract_version: int
    conditions: tuple[PlannedCondition, ...]
    provenance: dict[str, Any] = field(default_factory=dict)

@dataclass
class VerificationContext:
    run_id: str
    base_url: str | None = None
    workspace: str | None = None
    artifact_root: str = "artifacts/verification"
    timeout_seconds: float = 5.0
    credentials: dict[str, str] = field(default_factory=dict, repr=False)
    allowed_origin: str | None = None
