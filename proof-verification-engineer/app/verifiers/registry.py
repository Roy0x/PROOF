from __future__ import annotations
from .base import Verifier

class VerifierRegistry:
    def __init__(self, verifiers: dict[str, Verifier]) -> None:
        self._verifiers = dict(verifiers)
    def get(self, verifier_type: str) -> Verifier | None:
        return self._verifiers.get(verifier_type)
