from __future__ import annotations

from .base import Worker


class LocalCodingWorker(Worker):
    """Live Mode worker boundary; execution must be sandboxed before implementation."""
    def claim(self, goal: str) -> dict[str, str]:
        raise NotImplementedError("Local coding execution is not enabled in the Demo Mode MVP.")
