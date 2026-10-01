from __future__ import annotations

from abc import ABC, abstractmethod

from app.evidence.models import VerificationEvidence
from .models import VerificationContext, VerificationStep


class Verifier(ABC):
    @abstractmethod
    def verify(self, condition_id: str, step: VerificationStep, context: VerificationContext) -> VerificationEvidence:
        """Independently collect evidence for one fixed contract condition."""
