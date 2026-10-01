from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from time import monotonic

from .base import WorkerAdapter
from .models import ChangedFile, CommandResult, RepairRequest, WorkerClaim, WorkerExecutionResult, WorkerTask


class WorkspaceViolation(ValueError): pass
class CommandRejected(ValueError): pass


class LocalCodingWorkerAdapter(WorkerAdapter):
    """Deterministic worker restricted to disposable per-task workspaces."""
    _ALLOWED_MODULES = frozenset({"pytest", "compileall"})

    def __init__(self, workspace_root: Path | None = None, command_timeout: float = 10.0) -> None:
        self.workspace_root = (workspace_root or Path(tempfile.gettempdir()) / "proof-worker-workspaces").resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.command_timeout, self._results = command_timeout, {}

    def create_workspace(self, task: WorkerTask) -> Path:
        return Path(tempfile.mkdtemp(prefix=f"proof-{task.task_id}-", dir=self.workspace_root)).resolve()

    @staticmethod
    def build_child_environment() -> dict[str, str]:
        """Return only Windows/Python runtime variables, never app configuration.

        In particular, this deliberately does not inherit arbitrary parent values
        such as API keys, tokens, or values loaded from a developer's `.env` file.
        """
        inherited = os.environ
        names = (
            "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "OS",
            "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS", "TEMP", "TMP",
            "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA",
        )
        environment = {name: inherited[name] for name in names if inherited.get(name)}
        system_root = environment.get("SYSTEMROOT", r"C:\Windows")
        python_dir = str(Path(sys.executable).resolve().parent)
        environment["PATH"] = os.pathsep.join((python_dir, str(Path(system_root) / "System32"), system_root))
        environment["PYTHONIOENCODING"] = "utf-8"
        return environment

    def workspace_path(self, workspace: Path, path: str | Path) -> Path:
        workspace, candidate = workspace.resolve(), (workspace / path).resolve()
        try: candidate.relative_to(workspace)
        except ValueError as exc: raise WorkspaceViolation(f"Path escapes worker workspace: {path}") from exc
        return candidate

    def run_allowed_command(self, workspace: Path, command: tuple[str, ...]) -> CommandResult:
        workspace = workspace.resolve()
        try: workspace.relative_to(self.workspace_root)
        except ValueError as exc: raise WorkspaceViolation("Workspace is not assigned to this adapter") from exc
        if len(command) < 3 or Path(command[0]).resolve() != Path(sys.executable).resolve() or command[1] != "-m" or command[2] not in self._ALLOWED_MODULES:
            raise CommandRejected("Command is not in the local worker allowlist")
        started = monotonic()
        try:
            completed = subprocess.run(command, cwd=workspace, shell=False, text=True, capture_output=True, timeout=self.command_timeout, env=self.build_child_environment())
            return CommandResult(command, completed.stdout, completed.stderr, completed.returncode, monotonic() - started)
        except subprocess.TimeoutExpired as exc:
            return CommandResult(command, exc.stdout or "", exc.stderr or "", None, monotonic() - started, True)

    def run_task(self, task: WorkerTask) -> WorkerExecutionResult:
        workspace = self.create_workspace(task)
        self.workspace_path(workspace, "worker-task.txt").write_text(f"Controlled local worker task:\n{task.goal}\n", encoding="utf-8")
        result = self.run_allowed_command(workspace, (sys.executable, "-m", "compileall", "-q", "."))
        claim = WorkerClaim("COMPLETED" if result.exit_code == 0 else "FAILED", "Controlled workspace workflow completed.", task.task_id)
        execution = WorkerExecutionResult(task, str(workspace), claim, [ChangedFile("worker-task.txt", "Recorded deterministic worker task input.")], [result])
        self._results[task.task_id] = execution
        return execution

    def repair_task(self, request: RepairRequest) -> WorkerExecutionResult:
        original = self._results.get(request.task_id)
        if not original: raise KeyError(f"Unknown worker task: {request.task_id}")
        workspace = Path(original.workspace)
        self.workspace_path(workspace, "repair-request.txt").write_text("\n".join((request.instructions, *request.evidence)), encoding="utf-8")
        result = self.run_allowed_command(workspace, (sys.executable, "-m", "compileall", "-q", "."))
        execution = WorkerExecutionResult(original.task, str(workspace), WorkerClaim("COMPLETED", "Controlled repair workflow completed.", request.task_id), original.changed_files + [ChangedFile("repair-request.txt", "Recorded structured repair request.")], [result])
        self._results[request.task_id] = execution
        return execution

    def get_result(self, task_id: str) -> WorkerExecutionResult | None:
        return self._results.get(task_id)


LocalCodingWorker = LocalCodingWorkerAdapter
