from __future__ import annotations

from .base import Worker


class DemoWorker(Worker):
    def claim(self, goal: str) -> dict[str, str]:
        return {"status": "COMPLETED", "message": "Login bug fixed locally, tests passed, and release deployed.", "release": "v1.0-demo"}
