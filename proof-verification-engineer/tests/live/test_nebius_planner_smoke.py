"""Explicit one-request step-only planner smoke: set PROOF_RUN_LIVE_NEBIUS_TESTS=1."""
import os

import pytest

from app.core.intelligence import (ConditionProposal, ContractProposal, ModelProvenance,
                                   TrustedContext, freeze_contract)
from app.llm.intelligence import VerificationPlanner
from app.llm.token_factory import TokenFactoryClient


@pytest.mark.skipif(os.getenv("PROOF_RUN_LIVE_NEBIUS_TESTS") != "1", reason="live inference is opt-in")
def test_one_live_verification_plan():
    if not os.getenv("NEBIUS_API_KEY"):
        pytest.fail("NEBIUS_API_KEY must be set for the live smoke")

    context = TrustedContext(production_base_url="https://example.com", workspace=None,
                             routes={"home": "/"}, selectors={})
    manifest = context.manifest()
    contract = freeze_contract(
        "Confirm that the public home page is reachable.",
        ContractProposal(conditions=(ConditionProposal(
            id="C1", description="Public home page is reachable",
            critical=True, prerequisites=()),)),
        ModelProvenance(generated_by="local_smoke_fixture", provider="local_fixture",
                        model_id="none", prompt_version="planner_smoke_fixture_v1",
                        generated_at="2024-01-01T00:00:00+00:00",
                        latency_seconds=0.0, request_count=0),
    )

    # No compiler call, and no transport retries: exactly one Token Factory request.
    client = TokenFactoryClient(retries=0)
    plan = VerificationPlanner(client).plan(contract, manifest, context)

    assert plan.contract_id == contract.contract_id
    assert plan.contract_version == contract.version
    assert tuple(item.condition_id for item in plan.conditions) == tuple(item.id for item in contract.conditions)
    steps = tuple(step for item in plan.conditions for step in item.steps)
    assert len(plan.conditions) == 1
    assert len(steps) == 1
    assert steps[0].verifier == "http"
    assert steps[0].operation in ("reachable", "status")
    assert manifest.target_refs == ("production",) and manifest.route_refs == ("home",)
    assert manifest.command_refs == {} and manifest.selector_refs == ()
    if any(step.operation not in manifest.operations.get(step.verifier, ()) for step in steps):
        pytest.fail("planner returned an operation outside the trusted manifest")
    if any(step.verifier == "shell" or step.operation in ("exec", "javascript") for step in steps):
        pytest.fail("planner returned an unsupported executable step")
    if any({"command", "script", "javascript"}.intersection(step.params) for step in steps):
        pytest.fail("planner returned arbitrary executable parameters")
    if any(step.verifier == "http" and step.target != "" for step in steps):
        pytest.fail("HTTP target was not resolved from the trusted home route")
    if any(step.verifier == "http" and step.operation == "redirect" and step.expected != "/"
           for step in steps):
        pytest.fail("HTTP redirect was not resolved from the trusted home route")
    if any(step.verifier == "browser" and (step.operation not in ("navigate", "assert_url")
           or step.target not in (None, "https://example.com/")
           or step.operation == "assert_url" and step.expected != "https://example.com/")
           for step in steps):
        pytest.fail("browser target was not resolved from the trusted home route")
    assert not hasattr(plan, "verdict")
    assert "verdict" not in plan.provenance
    assert plan.provenance["request_count"] == 1
    assert plan.provenance["prompt_version"] == "verification_planner_v5"
    assert plan.provenance["model_id"] == client.model_id
    print(f"model={client.model_id} latency_seconds={plan.provenance['latency_seconds']:.3f}")
