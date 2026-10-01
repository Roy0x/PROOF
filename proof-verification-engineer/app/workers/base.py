from __future__ import annotations

from abc import ABC, abstractmethod

from .models import RepairRequest, WorkerExecutionResult, WorkerTask


class Worker(ABC):
    @abstractmethod
    def claim(self, goal: str) -> dict[str, str]:
        """Return a worker claim; PROOF never treats it as a verdict."""


class WorkerAdapter(ABC):
    """Worker claims are inputs to verification, never independent verdicts."""
    @abstractmethod
    def run_task(self, task: WorkerTask) -> WorkerExecutionResult: ...

    @abstractmethod
    def repair_task(self, request: RepairRequest) -> WorkerExecutionResult: ...

    @abstractmethod
    def get_result(self, task_id: str) -> WorkerExecutionResult | None: ...
