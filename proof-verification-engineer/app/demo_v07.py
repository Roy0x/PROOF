"""Unified v0.7 loop. Default is entirely offline; live requires two explicit gates."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from app.core.agent_verification_loop import ACCEPTANCE, REQUIRED_STEPS, AgentVerificationLoop
from app.llm.intelligence import SCENARIO_COMPILER_SYSTEM, SCENARIO_PLANNER_SYSTEM
from app.llm.token_factory import ModelResponse, ModelUsage, ReasoningPolicy, TokenFactoryClient
from app.workers.nemotron_coding import PATCH_SYSTEM, REPAIR_SYSTEM


_DEFAULT_CONTRACT = {"conditions": [
    {"id": "WEB_HOME_9", "acceptance_ref": "home", "description": "Deployed public home page responds with HTTP 200",
     "critical": True, "prerequisites": []},
    {"id": "DB_SCHEMA_2", "acceptance_ref": "schema", "description": "Production auth_sessions schema exists",
     "critical": True, "prerequisites": []},
    {"id": "AUTH_PROD_07", "acceptance_ref": "auth", "description": "Valid test user signs in to production",
     "critical": True, "prerequisites": []},
    {"id": "TEST_LOCAL_3", "acceptance_ref": "regression", "description": "Local regression tests pass",
     "critical": True, "prerequisites": []},
    {"id": "DASHBOARD_4", "acceptance_ref": "dashboard", "description": "Authenticated dashboard is visible",
     "critical": True, "prerequisites": ["AUTH_PROD_07"]},
]}
_DEFAULT_PATCH = {"action": "propose_code_patch", "file": "login_service.py",
                  "function": "password_matches", "replacement_expression": "candidate == stored"}
_DEFAULT_REPAIR = {"action": "apply_authorized_migration", "migration_ref": "auth_sessions_v1"}


class OfflineFullLoopModel:
    """Role-aware deterministic fixture with a strict eight-stage call-order audit."""

    model_id = "offline/mock-full-loop"

    def __init__(self, *, contract: dict | None = None, patch: dict | None = None,
                 steps: dict[str, dict] | None = None, repair: dict | None = None) -> None:
        self.contract = contract if contract is not None else _DEFAULT_CONTRACT
        self.patch = patch if patch is not None else _DEFAULT_PATCH
        self.steps = steps if steps is not None else {ref: {"steps": actions} for ref, actions in REQUIRED_STEPS.items()}
        self.repair = repair if repair is not None else _DEFAULT_REPAIR
        self.calls: list[str] = []

    def complete_json(self, system: str, user: str, *, max_output_tokens: int = 800,
                      reasoning_policy: ReasoningPolicy = ReasoningPolicy.PROVIDER_DEFAULT) -> ModelResponse:
        if reasoning_policy is not ReasoningPolicy.DISABLED or len(self.calls) >= 8:
            raise AssertionError("Offline fixture received unsafe or excess model call")
        index = len(self.calls)
        if index == 0 and system == PATCH_SYSTEM:
            role, data = "patch", self.patch
        elif index == 1 and system == SCENARIO_COMPILER_SYSTEM:
            request = json.loads(user)
            if tuple(request["trusted_acceptance_requirements"]) != ACCEPTANCE:
                # JSON round trip turns tuples into lists, but dict entries remain identical.
                if request["trusted_acceptance_requirements"] != list(ACCEPTANCE):
                    raise AssertionError("Trusted acceptance requirements changed")
            role, data = "compiler", self.contract
        elif 2 <= index <= 6 and system == SCENARIO_PLANNER_SYSTEM:
            request = json.loads(user)["condition"]
            ref = request["acceptance_ref"]
            if request["required_check"] != json.loads(json.dumps({"steps": REQUIRED_STEPS[ref]})):
                raise AssertionError("Trusted required check changed")
            role, data = f"planner:{ref}", self.steps[ref]
        elif index == 7 and system == REPAIR_SYSTEM:
            observation = json.loads(user)
            if (len(observation["failed_evidence"]) != 1
                    or observation["failed_evidence"][0]["expected"] != "True"
                    or observation["failed_evidence"][0]["observed"] != "False"):
                raise AssertionError("Repair was not grounded in failed evidence")
            role, data = "repair", self.repair
        else:
            raise AssertionError("Unexpected offline model stage or call order")
        self.calls.append(role)
        return ModelResponse(data, ModelUsage(self.model_id, 0, 0, 0.0, request_count=1, reasoning_tokens=0))


def run_demo(*, output_root: Path | None = None, live: bool = False,
             offline_model: OfflineFullLoopModel | None = None):
    if live:
        if os.getenv("PROOF_RUN_LIVE_FULL_LOOP") != "1":
            raise ValueError("Live full loop requires PROOF_RUN_LIVE_FULL_LOOP=1")
        if not os.getenv("NEBIUS_API_KEY"):
            raise ValueError("Live full loop requires NEBIUS_API_KEY")
        if offline_model is not None:
            raise ValueError("Live full loop cannot use an injected offline model")
        client = TokenFactoryClient(retries=0)
    else:
        client = offline_model or OfflineFullLoopModel()
    return AgentVerificationLoop(client, "live_nebius" if live else "offline_mock", output_root).run()


def main() -> None:
    parser = argparse.ArgumentParser(description="PROOF v0.7 integrated worker/contract/planner verification loop")
    parser.add_argument("--live", action="store_true", help="Requires PROOF_RUN_LIVE_FULL_LOOP=1 and NEBIUS_API_KEY")
    parser.add_argument("--output", type=Path, help="Temporary-directory root for durable Proof Packs")
    args = parser.parse_args()
    result = run_demo(output_root=args.output, live=args.live)
    print(f"MODEL EXECUTION: {result.execution_mode}")
    print(f"WORKER MODEL: {result.first.intelligence['worker_model_decisions'][0]['model_id']}")
    print(f"CODE ACTION: {result.worker_first.actions[0].outcome}")
    print(f"LOCAL TESTS: {'PASSED' if result.worker_first.command_results[-1].exit_code == 0 else 'FAILED'}")
    print(f"WORKER CLAIM: {result.worker_first.claim.status}")
    print(f"COMPILER: {result.prepared.contract.provenance.model_id} ({result.prepared.contract.provenance.prompt_version})")
    print(f"PLANNER: {result.prepared.plan.provenance['model_id']} ({result.prepared.plan.provenance['prompt_version']})")
    print(f"PROOF RUN 1: {result.first.verdict}")
    print(f"FAILED SCHEMA EVIDENCE: {result.repair_request.evidence[0]}")
    print(f"AI REPAIR DECISION: {result.second.intelligence['worker_model_decisions'][-1]['selected_value']}")
    print(f"REPAIR EXECUTION: {result.worker_repair.actions[0].outcome}")
    print(f"PROOF RUN 2: {result.second.verdict}")
    print(f"CONTRACT: {'unchanged' if result.first.contract_id == result.second.contract_id and result.first.contract_version == result.second.contract_version else 'changed'}")
    print(f"MODEL DECISIONS: {result.usage['attempted_requests']} attempted, {result.usage['completed_requests']} completed")
    print(f"TOKEN FACTORY REQUESTS: {result.usage['provider_requests_attempted']} attempted, {result.usage['provider_request_count']} completed")
    print(f"TOKENS: input={result.usage['prompt_tokens']} output={result.usage['completion_tokens']} reasoning={result.usage['reasoning_tokens']}")
    print(f"PROOF PACKS: {result.first_path} | {result.second_path}")


if __name__ == "__main__":
    main()
