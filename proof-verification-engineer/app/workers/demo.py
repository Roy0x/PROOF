from __future__ import annotations

from .base import Worker, WorkerAdapter
from .models import RepairRequest, WorkerClaim, WorkerExecutionResult, WorkerTask


class DemoWorker(Worker):
    def claim(self, goal: str) -> dict[str, str]:
        return {"status": "COMPLETED", "message": "Login bug fixed locally, tests passed, and release deployed.", "release": "v1.0-demo"}


class DemoWorkerAdapter(DemoWorker, WorkerAdapter):
    def __init__(self) -> None:
        self._results: dict[str, WorkerExecutionResult] = {}

    def run_task(self, task: WorkerTask) -> WorkerExecutionResult:
        data = self.claim(task.goal)
        result = WorkerExecutionResult(task, "demo://controlled-worker", WorkerClaim("COMPLETED", data["message"], task.task_id, data["release"]), [], [])
        self._results[task.task_id] = result
        return result

    def repair_task(self, request: RepairRequest) -> WorkerExecutionResult:
        return self.run_task(self._results[request.task_id].task)

    def get_result(self, task_id: str) -> WorkerExecutionResult | None:
        return self._results.get(task_id)
