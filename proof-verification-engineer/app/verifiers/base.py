from __future__ import annotations

from abc import ABC, abstractmethod

from app.core.contracts import Condition
from app.evidence.models import Evidence


class Verifier(ABC):
    @abstractmethod
    async def verify(self, condition: Condition) -> Evidence:
        """Independently collect evidence for one fixed contract condition."""
