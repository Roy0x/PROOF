from __future__ import annotations

from abc import ABC, abstractmethod


class Worker(ABC):
    @abstractmethod
    def claim(self, goal: str) -> dict[str, str]:
        """Return a worker claim; PROOF never treats it as a verdict."""
