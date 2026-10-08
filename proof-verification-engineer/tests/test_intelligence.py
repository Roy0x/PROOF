from copy import deepcopy
import json

import pytest

from app.core.intelligence import PlanningError, TrustedContext
from app.core.intelligence_workflow import IntelligenceWorkflow
from app.core.verification_engine import VerificationEngine
from app.evidence.models import VerificationEvidence
from app.llm.intelligence import (COMPILER_PROMPT_VERSION, DEFAULT_COMPILER_OUTPUT_TOKENS,
                                  PLANNER_SYSTEM, StepPlanProposal, OutcomeContractCompiler,
                                  VerificationPlanner, _model_capabilities)
from app.llm.token_factory import MAX_OUTPUT_TOKENS, ModelError, ModelResponse, ModelUsage, ReasoningPolicy
from app.storage.proof_packs_v3 import ProofPackStore
from app.verifiers.registry import VerifierRegistry


GOAL = "Fix the login bug and deploy the application."
CONTRACT = {"conditions": [
    {"id": "C1", "description": "Production application is reachable", "critical": True, "prerequisites": []},
    {"id": "C2", "description": "Valid test user reaches the dashboard", "critical": True, "prerequisites": ["C1"]},
    {"id": "C3", "description": "Regression tests pass", "critical": True, "prerequisites": []},
]}
PLAN = {"conditions": [
    {"condition_id": "C1", "steps": [{"verifier": "http", "operation": "status", "target_ref": "production", "route_ref": "home", "expected": 200}]},
    {"condition_id": "C2", "steps": [
        {"verifier": "browser", "operation": "navigate", "target_ref": "production", "route_ref": "login"},
        {"verifier": "browser", "operation": "fill", "target_ref": "production", "selector_ref": "password", "credential_ref": "valid_test_user.password"},
        {"verifier": "browser", "operation": "click", "target_ref": "production", "selector_ref": "submit"},
        {"verifier": "browser", "operation": "assert_url", "target_ref": "production", "route_ref": "dashboard"},
    ]},
    {"condition_id": "C3", "steps": [{"verifier": "shell", "operation": "pytest", "command_ref": "regression"}]},
]}


def plan_steps(index):
    return {"steps": deepcopy(PLAN["conditions"][index]["steps"])}


class FakeClient:
    model_id = "nvidia/Nemotron-3_5-Lightning"
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
    def complete_json(self, system, user, *, max_output_tokens=800,
                      reasoning_policy=ReasoningPolicy.PROVIDER_DEFAULT):
        self.calls.append((system, user, max_output_tokens, reasoning_policy))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return ModelResponse(result, ModelUsage(self.model_id, 20, 12, .01))


@pytest.fixture
def context(tmp_path):
    return TrustedContext(production_base_url="http://127.0.0.1:8080/demo-app", workspace=str(tmp_path),
        routes={"home": "/", "login": "/login", "dashboard": "/dashboard"},
        selectors={"password": "input[name=password]", "submit": "button[type=submit]"},
        commands={"regression": ("pytest", ("-q",))}, credentials={"valid_test_user.password": "fake-password"})


def prepared(context, contract=CONTRACT, plan=PLAN):
    fake = FakeClient(deepcopy(contract),
                      *({"steps": deepcopy(item["steps"])} for item in plan["conditions"]))
    workflow = IntelligenceWorkflow(fake, context)
    return workflow, fake, workflow.prepare(GOAL)


def frozen_with_ids(context, *ids):
    proposal = {"conditions": [
        {"id": condition_id, "description": f"Observable outcome for {condition_id}",
         "critical": True, "prerequisites": []}
        for condition_id in ids
    ]}
    return OutcomeContractCompiler(FakeClient(proposal)).compile(GOAL, context)


def reachable_steps():
    return {"steps": [{"verifier": "http", "operation": "reachable",
                       "target_ref": "production", "route_ref": "home"}]}


def test_compile_frozen_contract_and_safe_prompt(context):
    fake = FakeClient(deepcopy(CONTRACT))
    contract = OutcomeContractCompiler(fake).compile(GOAL, context)
    assert contract.goal == GOAL and contract.contract_id.startswith("contract_") and contract.version == 1
    assert [c.critical for c in contract.conditions] == [True] * 3
    assert contract.conditions[1].prerequisites == ("C1",)
    assert contract.provenance.provider == "nebius_token_factory"
    assert contract.provenance.prompt_version == "contract_compiler_v1"
    assert "fake-password" not in fake.calls[0][1]
    with pytest.raises(Exception):
        contract.conditions[0].description = "changed"


def test_default_stage_budgets_are_independent(context, monkeypatch):
    monkeypatch.delenv("PROOF_COMPILER_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("PROOF_PLANNER_MAX_OUTPUT_TOKENS", raising=False)
    _, fake, _ = prepared(context)
    assert [call[2] for call in fake.calls] == [1024, 2048, 2048, 2048]
    assert [call[3] for call in fake.calls] == [ReasoningPolicy.DISABLED] * 4


def test_configured_stage_budgets_are_independent(context, monkeypatch):
    monkeypatch.setenv("PROOF_COMPILER_MAX_OUTPUT_TOKENS", "1536")
    monkeypatch.setenv("PROOF_PLANNER_MAX_OUTPUT_TOKENS", "1792")
    _, fake, _ = prepared(context)
    assert [call[2] for call in fake.calls] == [1536, 1792, 1792, 1792]

    fake = FakeClient(deepcopy(CONTRACT), *(plan_steps(index) for index in range(3)))
    contract = OutcomeContractCompiler(fake, max_output_tokens=512).compile(GOAL, context)
    VerificationPlanner(fake, max_output_tokens=768).plan(contract, context.manifest(), context)
    assert [call[2] for call in fake.calls] == [512, 768, 768, 768]


def test_planner_reasoning_policy_is_independent(context):
    fake = FakeClient(deepcopy(CONTRACT), *(plan_steps(index) for index in range(3)))
    contract = OutcomeContractCompiler(fake).compile(GOAL, context)
    VerificationPlanner(fake, reasoning_policy=ReasoningPolicy.DISABLED).plan(
        contract, context.manifest(), context)
    assert [call[3] for call in fake.calls] == [ReasoningPolicy.DISABLED] * 4
    with pytest.raises(ValueError):
        VerificationPlanner(fake, reasoning_policy=ReasoningPolicy.PROVIDER_DEFAULT)
    with pytest.raises(ValueError):
        VerificationPlanner(fake, reasoning_policy={"chat_template_kwargs": {"enable_thinking": True}})


@pytest.mark.parametrize("stage,env_name", [
    (OutcomeContractCompiler, "PROOF_COMPILER_MAX_OUTPUT_TOKENS"),
    (VerificationPlanner, "PROOF_PLANNER_MAX_OUTPUT_TOKENS"),
])
def test_stage_budget_cannot_exceed_hard_cap(stage, env_name, monkeypatch):
    with pytest.raises(ValueError):
        stage(FakeClient(), max_output_tokens=MAX_OUTPUT_TOKENS + 1)
    monkeypatch.setenv(env_name, str(MAX_OUTPUT_TOKENS + 1))
    with pytest.raises(ValueError):
        stage(FakeClient())


@pytest.mark.parametrize("change", [
    lambda x: x["conditions"].append(deepcopy(x["conditions"][0])),
    lambda x: x["conditions"][0].pop("critical"),
    lambda x: x["conditions"][1].update(prerequisites=["missing"]),
    lambda x: x.update(conditions=[]),
    lambda x: x["conditions"][0].update(description="The worker says deployment succeeded"),
    lambda x: x.update(contract_id="model-chosen"),
    lambda x: x["conditions"][0].update(critical="true"),
])
def test_invalid_contracts_fail_closed(context, change):
    proposal = deepcopy(CONTRACT); change(proposal)
    with pytest.raises(PlanningError) as exc:
        OutcomeContractCompiler(FakeClient(proposal)).compile(GOAL, context)
    assert exc.value.stage == "compilation"


def test_model_cannot_choose_authoritative_contract_id(context):
    first = OutcomeContractCompiler(FakeClient(deepcopy(CONTRACT))).compile(GOAL, context)
    second = OutcomeContractCompiler(FakeClient(deepcopy(CONTRACT))).compile(GOAL, context)
    assert first.contract_id != second.contract_id


def test_configured_secret_in_goal_is_rejected_before_model_call(context, monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "fake-api-key")
    fake = FakeClient(deepcopy(CONTRACT))
    with pytest.raises(PlanningError) as exc:
        OutcomeContractCompiler(fake).compile("Deploy using fake-api-key", context)
    assert exc.value.code == "secret_in_goal" and fake.calls == []


def test_plan_accepts_bounded_http_browser_shell_and_provenance(context):
    workflow, fake, item = prepared(context)
    assert [c.condition_id for c in item.plan.conditions] == ["C1", "C2", "C3"]
    assert item.plan.conditions[1].prerequisites == ("C1",)
    assert item.plan.conditions[1].steps[0].target == "http://127.0.0.1:8080/demo-app/login"
    assert item.plan.conditions[1].steps[1].expected is None
    assert item.plan.conditions[1].steps[1].params == {"credential_ref": "valid_test_user.password"}
    assert item.plan.conditions[1].steps[3].params == {"exact": True}
    assert item.plan.conditions[2].steps[0].params == {"args": ("-q",)}
    assert item.plan.provenance["prompt_version"] == "verification_planner_v5"
    assert item.plan.provenance["request_count"] == 3
    assert item.plan.provenance["prompt_tokens"] == 60
    assert all("fake-password" not in call[1] for call in fake.calls[1:])


def test_planner_prompt_is_step_only():
    assert "ONE selected frozen condition" in PLANNER_SYSTEM
    assert "exactly a steps array" in PLANNER_SYSTEM
    assert "Never output conditions, condition_id, contract_id, verdict" in PLANNER_SYSTEM
    assert '"steps":[{"verifier":"http","operation":"reachable"' in PLANNER_SYSTEM


def test_planner_prompt_requires_minimal_bounded_output():
    assert "minimum sufficient steps" in PLANNER_SYSTEM
    assert "prefer ONE deterministic step" in PLANNER_SYSTEM
    assert "at most 8 steps" in PLANNER_SYSTEM
    assert "No reasoning, commentary, or prose outside JSON" in PLANNER_SYSTEM
    assert "redundant HTTP, browser, or shell checks" in PLANNER_SYSTEM
    assert "Do not invent metadata or explanations" in PLANNER_SYSTEM
    assert COMPILER_PROMPT_VERSION == "contract_compiler_v1"
    assert DEFAULT_COMPILER_OUTPUT_TOKENS == 1024


def test_simple_reachability_fixture_is_one_step_and_compact(context):
    proposal = reachable_steps()
    parsed = StepPlanProposal.model_validate(proposal)
    assert len(parsed.steps) == 1
    assert parsed.steps[0].verifier == "http"
    assert parsed.steps[0].operation == "reachable"
    assert len(json.dumps(proposal, separators=(",", ":")).encode("utf-8")) <= 160
    contract = frozen_with_ids(context, "C1")
    plan = VerificationPlanner(FakeClient(proposal)).plan(contract, context.manifest(), context)
    assert len(plan.conditions) == 1 and len(plan.conditions[0].steps) == 1


def test_two_verification_steps_stay_inside_one_frozen_condition(context):
    contract = frozen_with_ids(context, "C1")
    response = reachable_steps()
    response["steps"].append(
        {"verifier": "http", "operation": "status", "target_ref": "production",
         "route_ref": "home", "expected": 200})
    plan = VerificationPlanner(FakeClient(response)).plan(contract, context.manifest(), context)
    assert [item.condition_id for item in plan.conditions] == ["C1"]
    assert [step.operation for step in plan.conditions[0].steps] == ["reachable", "status"]


def test_multi_step_browser_flow_remains_allowed_when_needed(context):
    contract_proposal = {"conditions": [{"id": "AUTH-PROD-07",
        "description": "Valid login reaches the dashboard", "critical": True, "prerequisites": []}]}
    contract = OutcomeContractCompiler(FakeClient(contract_proposal)).compile(GOAL, context)
    response = plan_steps(1)
    plan = VerificationPlanner(FakeClient(response)).plan(contract, context.manifest(), context)
    assert len(plan.conditions[0].steps) == 4


def test_model_capabilities_compact_view_preserves_trusted_authority(context):
    manifest = context.manifest()
    model_view = _model_capabilities(manifest)
    assert set(model_view) == {"operations", "target_refs", "route_refs", "selector_refs",
                               "command_refs", "credential_refs"}
    for name in model_view:
        assert model_view[name] == getattr(manifest, name)
    contract = frozen_with_ids(context, "C1")
    fake = FakeClient(reachable_steps())
    VerificationPlanner(fake).plan(contract, manifest, context)
    sent = fake.calls[0][1]
    assert sent == json.dumps(json.loads(sent), sort_keys=True, separators=(",", ":"))
    assert json.loads(sent)["capabilities"]["operations"] == {
        name: list(operations) for name, operations in manifest.operations.items()}
    assert "version" not in json.loads(sent)["capabilities"]
    sent_condition = json.loads(sent)["condition"]
    assert sent_condition == {"description": "Observable outcome for C1", "prerequisites": []}
    assert "contract" not in json.loads(sent)
    assert "condition_id" not in sent


def test_oversized_one_condition_plan_remains_schema_invalid(context):
    contract = frozen_with_ids(context, "C1")
    oversized = reachable_steps()
    oversized["steps"] *= 9
    with pytest.raises(PlanningError) as exc:
        VerificationPlanner(FakeClient(oversized)).plan(contract, context.manifest(), context)
    assert exc.value.code == "invalid_plan"


@pytest.mark.parametrize("ids", [("C1",), ("AUTH-PROD-07", "PAYMENT_2")])
def test_exact_frozen_condition_ids_succeed_without_c1_assumption(context, ids):
    contract = frozen_with_ids(context, *ids)
    fake = FakeClient(*(reachable_steps() for _ in ids))
    plan = VerificationPlanner(fake).plan(contract, context.manifest(), context)
    assert [item.condition_id for item in plan.conditions] == list(ids)
    assert len(fake.calls) == len(ids)
    assert all(set(json.loads(call[1])) == {"condition", "capabilities"} for call in fake.calls)
    assert [json.loads(call[1])["condition"]["description"] for call in fake.calls] == [
        f"Observable outcome for {condition_id}" for condition_id in ids]
    assert plan.contract_id == contract.contract_id and plan.contract_version == contract.version


@pytest.mark.parametrize("extra", [
    {"condition_id": "C1"},
    {"contract_id": "model-chosen"},
    {"conditions": [{"condition_id": "C2", "steps": []}]},
    {"overall_verdict": "VERIFIED"},
    {"description": "Altered frozen condition"},
    {"prerequisites": []},
])
def test_planner_rejects_model_owned_structure(context, extra):
    contract = frozen_with_ids(context, "C1")
    response = {**reachable_steps(), **extra}
    with pytest.raises(PlanningError) as exc:
        VerificationPlanner(FakeClient(response)).plan(contract, context.manifest(), context)
    assert exc.value.code == "invalid_plan"


def test_planner_assembles_nontrivial_ids_in_frozen_order(context):
    contract = frozen_with_ids(context, "AUTH-PROD-07", "PAYMENT_2")
    second = {"steps": [{"verifier": "http", "operation": "status", "target_ref": "production",
                         "route_ref": "home", "expected": 200}]}
    plan = VerificationPlanner(FakeClient(reachable_steps(), second)).plan(
        contract, context.manifest(), context)
    assert [item.condition_id for item in plan.conditions] == ["AUTH-PROD-07", "PAYMENT_2"]
    assert [item.steps[0].operation for item in plan.conditions] == ["reachable", "status"]


def test_planner_receives_prerequisite_descriptions_without_identity_fields(context):
    proposal = {"conditions": [
        {"id": "AUTH-PROD-07", "description": "Login endpoint accepts a test user",
         "critical": True, "prerequisites": []},
        {"id": "PAYMENT_2", "description": "Dashboard appears after login",
         "critical": True, "prerequisites": ["AUTH-PROD-07"]},
    ]}
    contract = OutcomeContractCompiler(FakeClient(proposal)).compile(GOAL, context)
    fake = FakeClient(reachable_steps(), reachable_steps())
    plan = VerificationPlanner(fake).plan(contract, context.manifest(), context)
    second_input = json.loads(fake.calls[1][1])
    assert second_input["condition"] == {
        "description": "Dashboard appears after login",
        "prerequisites": ["Login endpoint accepts a test user"]}
    assert "AUTH-PROD-07" not in fake.calls[1][1]
    assert plan.conditions[1].prerequisites == ("AUTH-PROD-07",)


def test_failed_second_condition_does_not_publish_partial_plan(context):
    fake = FakeClient(deepcopy(CONTRACT), reachable_steps(),
                      ModelError("read_timeout", "Token Factory request timed out (read_timeout)"))
    workflow = IntelligenceWorkflow(fake, context)
    with pytest.raises(PlanningError) as exc:
        workflow.prepare(GOAL)
    assert exc.value.stage == "planning" and exc.value.code == "read_timeout"
    assert len(fake.calls) == 3
    assert workflow._prepared_hashes == {}


def test_step_schema_rejects_nested_condition_identity(context):
    contract = frozen_with_ids(context, "C1")
    response = reachable_steps()
    response["steps"][0]["condition_id"] = "C2"
    with pytest.raises(PlanningError) as exc:
        VerificationPlanner(FakeClient(response)).plan(contract, context.manifest(), context)
    assert exc.value.code == "invalid_plan"


def test_matching_ids_do_not_bypass_capability_validation(context):
    contract = frozen_with_ids(context, "AUTH-PROD-07")
    malicious = {"steps": [{"verifier": "shell", "operation": "exec",
                             "command_ref": "regression"}]}
    with pytest.raises(PlanningError) as exc:
        VerificationPlanner(FakeClient(malicious)).plan(contract, context.manifest(), context)
    assert exc.value.code == "unsupported_step"


@pytest.mark.parametrize("step", [
    {"verifier": "unknown", "operation": "status", "target_ref": "production", "route_ref": "home", "expected": 200},
    {"verifier": "http", "operation": "post", "target_ref": "production", "route_ref": "home"},
    {"verifier": "shell", "operation": "exec", "command": "powershell.exe -Command evil"},
    {"verifier": "browser", "operation": "javascript", "target_ref": "production", "script": "fetch('/secret')"},
    {"verifier": "http", "operation": "status", "target_ref": "http://169.254.169.254", "route_ref": "home", "expected": 200},
    {"verifier": "browser", "operation": "navigate", "target_ref": "production", "route_ref": "http://169.254.169.254"},
    {"verifier": "shell", "operation": "pytest", "command_ref": "regression", "args": ["-c", "evil"]},
])
def test_malicious_steps_rejected_before_verifier(context, step):
    proposal = {"steps": [step]}
    contract = frozen_with_ids(context, "C1")
    with pytest.raises(PlanningError):
        VerificationPlanner(FakeClient(proposal)).plan(contract, context.manifest(), context)


@pytest.mark.parametrize("change", [
    lambda x: x.update(overall_verdict="VERIFIED"),
    lambda x: x.update(condition_id="other"),
    lambda x: x.update(steps=[]),
    lambda x: x["steps"][0].update(target_ref="internal"),
])
def test_plan_cannot_override_verdict_or_contract(context, change):
    proposal = reachable_steps(); change(proposal)
    contract = frozen_with_ids(context, "C1")
    with pytest.raises(PlanningError):
        VerificationPlanner(FakeClient(proposal)).plan(contract, context.manifest(), context)


def test_manifest_mismatch_rejected(context):
    contract = OutcomeContractCompiler(FakeClient(deepcopy(CONTRACT))).compile(GOAL, context)
    manifest = context.manifest().model_copy(update={"target_refs": ("internal",)})
    with pytest.raises(PlanningError):
        VerificationPlanner(FakeClient(reachable_steps())).plan(contract, manifest, context)


def test_prepared_plan_mutation_is_rejected(context):
    workflow, _, item = prepared(context)
    item.plan.conditions[2].steps[0].params["args"] = ("-c", "evil")
    with pytest.raises(PlanningError) as exc:
        workflow.verify(item, VerificationEngine(VerifierRegistry({})), "run_tampered")
    assert exc.value.code == "plan_changed"


def test_reverification_uses_same_contract_fresh_evidence_and_no_model_call(context, tmp_path):
    workflow, fake, item = prepared(context)
    class Toggle:
        failed = True
        def verify(self, condition_id, step, execution):
            status = "FAILED" if self.failed and condition_id == "C2" else "VERIFIED"
            return VerificationEvidence(condition_id, step.verifier, step.operation, step.expected,
                                        "observed", status, execution.run_id)
    toggle = Toggle()
    engine = VerificationEngine(VerifierRegistry({"http": toggle, "browser": toggle, "shell": toggle}))
    store = ProofPackStore(tmp_path / "packs")
    first = workflow.verify(item, engine, "run_1"); store.save(first)
    toggle.failed = False
    second = workflow.verify(item, engine, "run_2"); store.save(second)
    assert first.verdict == "FAILED" and second.verdict == "VERIFIED"
    assert first.contract_id == second.contract_id == item.contract.contract_id
    assert first.contract_version == second.contract_version == 1
    assert first.evidence[0].evidence_id != second.evidence[0].evidence_id
    assert store.load("run_1")["verdict"] == "FAILED"
    assert len(fake.calls) == 4
    assert first.intelligence["contract"]["provenance"]["generated_by"] == "nemotron"
    assert first.intelligence["plan_provenance"]["provider"] == "nebius_token_factory"
    assert "fake-password" not in (tmp_path / "packs" / "run_1.json").read_text()
