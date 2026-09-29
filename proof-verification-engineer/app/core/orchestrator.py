from __future__ import annotations

from typing import Awaitable, Callable

from app.core.contracts import contract_to_dict
from app.evidence.models import report_to_dict
from app.llm.nebius import generate_contract_with_nebius
from app.workers.demo import DemoWorker


async def run_demo(goal: str, base_url: str, verify: Callable[..., Awaitable], repair: Callable[[], None]) -> dict:
    """Freeze one contract, verify it, repair from evidence, then replay it unchanged."""
    contract = generate_contract_with_nebius(goal)
    first = await verify(base_url, contract)
    repair_info = None
    if first.verdict != "VERIFIED":
        repair_info = {"diagnosis": "Production authentication failed despite a successful deployment claim.",
                       "evidence_forwarded": [{"condition": e.condition_name, "expected": e.expected, "observed": e.observed} for e in first.evidence if e.verdict != "PASS"],
                       "worker_action": "Applied missing production auth schema migration and redeployed."}
        repair()
    final = await verify(base_url, contract)
    return {"goal": goal, "worker_claim": DemoWorker().claim(goal), "contract": contract_to_dict(contract),
            "first_verification": report_to_dict(first), "repair": repair_info,
            "final_verification": report_to_dict(final),
            "principle": "The worker does the task. PROOF independently verifies the resulting state."}
