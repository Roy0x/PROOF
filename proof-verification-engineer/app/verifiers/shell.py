from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.evidence.models import VerificationEvidence
from app.workers.local_coding import CommandRejected, LocalCodingWorkerAdapter, WorkspaceViolation
from .base import Verifier
from .models import VerificationContext, VerificationStep

class ShellVerifier(Verifier):
    _OPERATIONS = frozenset({"pytest", "compileall"})
    def verify(self, condition_id: str, step: VerificationStep, context: VerificationContext) -> VerificationEvidence:
        common = dict(condition_id=condition_id, verifier_type="shell", operation=step.operation, expected=step.expected, run_id=context.run_id, timestamp=datetime.now(timezone.utc).isoformat())
        if step.operation not in self._OPERATIONS or not context.workspace:
            return VerificationEvidence(**common, observed=None, status="BLOCKED", error="Unsupported shell operation or missing workspace")
        worker = LocalCodingWorkerAdapter(Path(context.workspace).parent, context.timeout_seconds)
        command = (str(__import__("sys").executable), "-m", step.operation, *tuple(step.params.get("args", ())))
        try:
            result = worker.run_allowed_command(Path(context.workspace), command)
            status = "INCONCLUSIVE" if result.timed_out else ("VERIFIED" if result.exit_code == step.expected else "FAILED")
            return VerificationEvidence(**common, observed=result.exit_code, status=status, duration_seconds=result.duration_seconds, metadata={"command": command, "cwd": context.workspace, "stdout": result.stdout[:4000], "stderr": result.stderr[:4000], "timed_out": result.timed_out})
        except (CommandRejected, WorkspaceViolation) as exc:
            return VerificationEvidence(**common, observed=None, status="BLOCKED", error=str(exc))

RestrictedShellVerifier = ShellVerifier
