from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import Literal
from uuid import uuid4

@dataclass(frozen=True)
class WorkerTask:
    goal: str
    task_id: str = field(default_factory=lambda: uuid4().hex)

@dataclass(frozen=True)
class ChangedFile:
    path: str
    summary: str

@dataclass(frozen=True)
class CommandResult:
    command: tuple[str, ...]
    stdout: str
    stderr: str
    exit_code: int | None
    duration_seconds: float
    timed_out: bool = False

@dataclass(frozen=True)
class WorkerClaim:
    status: Literal["COMPLETED", "FAILED", "BLOCKED"]
    message: str
    task_id: str
    release: str | None = None

@dataclass(frozen=True)
class FailedEvidenceReference:
    evidence_id: str
    condition_id: str
    operation: str
    expected: str
    observed: str
    status: Literal["FAILED"]

@dataclass(frozen=True)
class RepairRequest:
    task_id: str
    evidence: tuple[str, ...]
    instructions: str
    failed_evidence: tuple[FailedEvidenceReference, ...] = ()

@dataclass(frozen=True)
class WorkerAction:
    name: str
    target: str
    outcome: str
    duration_seconds: float

@dataclass
class WorkerExecutionResult:
    task: WorkerTask
    workspace: str
    claim: WorkerClaim
    changed_files: list[ChangedFile]
    command_results: list[CommandResult]
    started_at: float = field(default_factory=monotonic)
    actions: list[WorkerAction] = field(default_factory=list)
