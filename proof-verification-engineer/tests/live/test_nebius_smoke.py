"""Explicit one-request live smoke: set PROOF_RUN_LIVE_NEBIUS_TESTS=1."""
import os

import pytest

from app.core.intelligence import TrustedContext
from app.llm.intelligence import OutcomeContractCompiler
from app.llm.token_factory import TokenFactoryClient


@pytest.mark.skipif(os.getenv("PROOF_RUN_LIVE_NEBIUS_TESTS") != "1", reason="live inference is opt-in")
def test_one_live_contract_compilation():
    assert os.getenv("NEBIUS_API_KEY"), "NEBIUS_API_KEY must be set for the live smoke"
    context = TrustedContext(production_base_url="https://example.com", workspace=None,
                             routes={"home": "/"}, selectors={})
    contract = OutcomeContractCompiler(TokenFactoryClient(retries=0)).compile(
        "Confirm that the public home page is reachable.", context)
    assert contract.conditions and contract.provenance.model_id
    print(f"model={contract.provenance.model_id} latency_seconds={contract.provenance.latency_seconds:.3f}")
