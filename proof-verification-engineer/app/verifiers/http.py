from __future__ import annotations

from app.core.contracts import OutcomeContract
from app.evidence.models import VerificationReport


async def verify_http_contract(base_url: str, contract: OutcomeContract) -> VerificationReport:
    # Implementation remains in the compatibility module during this mechanical move.
    from app.engine import verify_contract
    return await verify_contract(base_url, contract)
