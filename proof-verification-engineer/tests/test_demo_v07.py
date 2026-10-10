"""Offline-only integration and adversarial checks for the unified v0.7 boundary."""
from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest

from app import demo_v07
from app.core.agent_verification_loop import (
    ACCEPTANCE, MAX_MODEL_REQUESTS, REQUIRED_STEPS, AgentVerificationLoop,
    RequestBudgetClient, repair_request_from_evidence, scenario_context,
    validate_scenario_contract, validate_scenario_plan,
)
from app.core.intelligence import PlanningError
from app.core.intelligence import TrustedContext
from app.core.intelligence_workflow import IntelligenceWorkflow
from app.demo_v07 import OfflineFullLoopModel, run_demo
from app.llm.intelligence import (SCENARIO_COMPILER_PROMPT_VERSION, SCENARIO_COMPILER_SYSTEM,
                                  SCENARIO_PLANNER_PROMPT_VERSION, SCENARIO_PLANNER_SYSTEM,
                                  OutcomeContractCompiler, StepProposal, VerificationPlanner)
from app.llm.token_factory import ModelError, ModelResponse, ModelUsage, ReasoningPolicy
from app.storage.proof_packs_v3 import ProofPackStore
from app.workers.nemotron_coding import WorkerDecisionError


@pytest.fixture(scope="module")
def complete_demo(tmp_path_factory):
    return run_demo(output_root=tmp_path_factory.mktemp("proof-v07-packs"))


def _contract_with(conditions):
    return {"conditions": conditions}


def test_unified_offline_loop_uses_real_evidence_and_dynamic_ids(complete_demo):
    result = complete_demo
    assert result.execution_mode == "offline_mock"
    assert result.worker_first.command_results[0].exit_code != 0
    assert result.worker_first.command_results[1].exit_code == 0
    assert result.worker_first.claim.status == "COMPLETED"
    assert [condition.id for condition in result.prepared.contract.conditions] == [
        "WEB_HOME_9", "DB_SCHEMA_2", "AUTH_PROD_07", "TEST_LOCAL_3", "DASHBOARD_4"]
    assert [condition.acceptance_ref for condition in result.prepared.contract.conditions] == [
        "home", "schema", "auth", "regression", "dashboard"]
    assert result.first.verdict == "FAILED" and result.second.verdict == "VERIFIED"
    assert {item.condition_id: item.status for item in result.first.condition_results}["DASHBOARD_4"] == "BLOCKED"
    assert result.first.contract_id == result.second.contract_id
    assert result.first.contract_version == result.second.contract_version
    assert result.first.intelligence["contract"] == result.second.intelligence["contract"]
    assert result.first.intelligence["plan"] == result.second.intelligence["plan"]
    assert result.first.intelligence["plan_fingerprint"] == result.second.intelligence["plan_fingerprint"]
    assert {item.evidence_id for item in result.first.evidence}.isdisjoint(
        {item.evidence_id for item in result.second.evidence})
    assert json.loads(result.first_path.read_text())["verdict"] == "FAILED"
    assert json.loads(result.second_path.read_text())["verdict"] == "VERIFIED"
    assert not result.workspace_path.exists()


def test_exact_coverage_and_model_provenance(complete_demo):
    result = complete_demo
    for contract_condition, planned in zip(result.prepared.contract.conditions, result.prepared.plan.conditions):
        assert planned.condition_id == contract_condition.id
        assert planned.description == contract_condition.description
        assert len(planned.steps) == len(REQUIRED_STEPS[contract_condition.acceptance_ref])
    schema_id = next(item.id for item in result.prepared.contract.conditions if item.acceptance_ref == "schema")
    probe = next(item for item in result.first.evidence if item.condition_id == schema_id and item.operation == "json_value")
    assert probe.status == "FAILED" and probe.expected is True and probe.observed is False
    assert probe.metadata["url"].endswith("/health/schema")
    assert probe.evidence_id == result.repair_request.evidence[0]
    assert result.repair_request.failed_evidence[0].condition_id == schema_id
    assert result.repair_request.failed_evidence[0].route_ref == "schema"
    assert result.repair_request.failed_evidence[0].json_path == "auth_sessions_exists"
    assert result.prepared.contract.provenance.prompt_version == SCENARIO_COMPILER_PROMPT_VERSION
    assert result.prepared.plan.provenance["prompt_version"] == SCENARIO_PLANNER_PROMPT_VERSION
    assert result.usage["attempted_requests"] == result.usage["completed_requests"] == 8
    assert result.usage["provider_requests_attempted"] == result.usage["provider_request_count"] == 0
    assert result.first.intelligence["model_usage"]["attempted_requests"] == 7
    assert result.second.intelligence["model_usage"]["attempted_requests"] == 8
    assert len(result.first.intelligence["worker_model_decisions"]) == 1
    assert len(result.second.intelligence["worker_model_decisions"]) == 2


def test_offline_fixture_call_order_and_reasoning_policy():
    model = OfflineFullLoopModel()
    # Full execution is covered by the shared integration fixture. This separate
    # fixture test verifies the first decision does not silently use provider defaults.
    response = model.complete_json("Return one JSON object only: action=propose_code_patch, file=login_service.py, "
        "function=password_matches, replacement_expression=a Python expression using only "
        "candidate and stored with == or !=. You may replace ONLY the return expression in "
        "password_matches. No code blocks, imports, calls, explanations, or verdicts. "
        "Use the failing local test to choose the repair.", "{}",
        max_output_tokens=256, reasoning_policy=ReasoningPolicy.DISABLED)
    assert response.data["replacement_expression"] == "candidate == stored"
    assert model.calls == ["patch"]
    with pytest.raises(AssertionError):
        model.complete_json("unexpected", "{}", reasoning_policy=ReasoningPolicy.DISABLED)


def test_contract_missing_required_outcome_stops_before_planner(tmp_path):
    base = deepcopy(demo_v07._DEFAULT_CONTRACT["conditions"])
    missing = _contract_with([item for item in base if item["acceptance_ref"] != "dashboard"])
    model = OfflineFullLoopModel(contract=missing)
    with pytest.raises(PlanningError) as error:
        run_demo(output_root=tmp_path / "packs", offline_model=model)
    assert error.value.code == "insufficient_contract"
    assert model.calls == ["patch", "compiler"]


def _compile_scenario_fixture(conditions, *, prepare=False):
    class ContractOnlyModel:
        model_id = "offline/prerequisite-fixture"

        def __init__(self):
            self.calls = []

        def complete_json(self, system, user, **_kwargs):
            self.calls.append((system, json.loads(user)))
            if len(self.calls) != 1:
                raise AssertionError("Planner called after a rejected contract")
            return ModelResponse(_contract_with(conditions),
                                 ModelUsage(self.model_id, 0, 0, 0.0))

    model = ContractOnlyModel()
    context = TrustedContext(production_base_url="https://example.com", workspace=None,
                             routes={"home": "/"}, selectors={})
    if prepare:
        return model, lambda: IntelligenceWorkflow(model, context).prepare(
            "Verify the disposable login deployment", trusted_requirements=ACCEPTANCE,
            contract_guard=validate_scenario_contract)
    return model, lambda: OutcomeContractCompiler(model).compile(
        "Verify the disposable login deployment", context, trusted_requirements=ACCEPTANCE)


def test_scenario_compiler_prompt_specifies_exact_prerequisite_graph():
    prompt = SCENARIO_COMPILER_SYSTEM
    assert "home, auth, schema, and regression" in prompt
    assert "prerequisites MUST be []" in prompt
    assert "dashboard MUST have prerequisites containing ONLY the id" in prompt
    assert "Put auth before dashboard" in prompt
    assert 'dashboard prerequisites must be ["LOGIN_7"]' in prompt


def test_independent_checks_and_dynamic_compiler_ids_are_accepted():
    conditions = deepcopy(demo_v07._DEFAULT_CONTRACT["conditions"])
    ids = {"home": "PUBLIC_73", "schema": "SESSION_DB_A", "auth": "LOGIN_824",
           "regression": "TESTS_6", "dashboard": "AFTER_LOGIN_Z"}
    for item in conditions:
        item["id"] = ids[item["acceptance_ref"]]
        item["prerequisites"] = [ids["auth"]] if item["acceptance_ref"] == "dashboard" else []
    model, compile_contract = _compile_scenario_fixture(conditions)
    contract = compile_contract()
    validate_scenario_contract(contract)
    assert [item.id for item in contract.conditions] == [ids[item["acceptance_ref"]] for item in conditions]
    assert all(not item.prerequisites for item in contract.conditions if item.acceptance_ref != "dashboard")
    assert next(item for item in contract.conditions if item.acceptance_ref == "dashboard").prerequisites == (ids["auth"],)
    assert contract.provenance.prompt_version == SCENARIO_COMPILER_PROMPT_VERSION
    assert model.calls[0][0] == SCENARIO_COMPILER_SYSTEM


@pytest.mark.parametrize("ref,source", [("home", "schema"), ("schema", "home"),
                                         ("auth", "schema"), ("regression", "auth")])
def test_independent_prerequisite_is_rejected_before_planner(ref, source):
    conditions = deepcopy(demo_v07._DEFAULT_CONTRACT["conditions"])
    by_ref = {item["acceptance_ref"]: item for item in conditions}
    if ref == "home":
        conditions = [by_ref[name] for name in ("schema", "home", "auth", "regression", "dashboard")]
    by_ref[ref]["prerequisites"] = [by_ref[source]["id"]]
    model, prepare = _compile_scenario_fixture(conditions, prepare=True)
    with pytest.raises(PlanningError) as error:
        prepare()
    assert error.value.stage == "compilation" and error.value.code == "unsafe_prerequisite"
    assert model.calls and len(model.calls) == 1
    message = str(error.value)
    assert f'"condition_id": "{by_ref[ref]["id"]}"' in message
    assert f'"acceptance_ref": "{ref}"' in message
    assert f'"rejected_prerequisite_ids": ["{by_ref[source]["id"]}"]' in message
    assert "fake-test-password" not in message and "Authorization" not in message


def test_dashboard_requires_only_generated_auth_id_before_planner():
    conditions = deepcopy(demo_v07._DEFAULT_CONTRACT["conditions"])
    conditions[-1]["prerequisites"] = [conditions[2]["id"], conditions[0]["id"]]
    model, prepare = _compile_scenario_fixture(conditions, prepare=True)
    with pytest.raises(PlanningError) as error:
        prepare()
    assert error.value.code == "invalid_dependency"
    assert len(model.calls) == 1


def _planner_context(tmp_path):
    return TrustedContext(production_base_url="http://127.0.0.1:8000", workspace=str(tmp_path),
        routes={"home": "/", "login": "/login", "dashboard": "/dashboard",
                "schema": "/health/schema"},
        selectors={"password": "#password", "submit": "#submit",
                   "dashboard_heading": "#dashboard-heading"},
        commands={"regression": ("pytest", ("-q",)), "syntax": ("compileall", ())},
        credentials={"valid_test_user.password": "fake-test-password"})


def test_scenario_planner_prompt_has_exact_allowed_field_combinations():
    prompt = SCENARIO_PLANNER_SYSTEM
    for snippet in ("condition.required_check.steps", "same order", "do not add optional fields",
                    "home: http/status", "auth: browser/navigate", "browser/fill",
                    "browser/click", "browser/wait_for", "dashboard: browser/assert_text",
                    "schema: http/json_value", "expected=true (JSON boolean)",
                    "regression: shell/pytest with command_ref=regression ONLY",
                    "omit target_ref", "Use only refs listed in the supplied manifest"):
        assert snippet in prompt


def test_every_required_acceptance_step_is_authorized_by_manifest(tmp_path):
    context = _planner_context(tmp_path)
    manifest = context.manifest()
    expected_operations = {"home": ["status"],
                           "auth": ["navigate", "fill", "click", "wait_for"],
                           "dashboard": ["assert_text"], "schema": ["json_value"],
                           "regression": ["pytest"]}
    for ref, steps in REQUIRED_STEPS.items():
        validated = [VerificationPlanner._validate_step(StepProposal.model_validate(step), manifest, context)
                     for step in steps]
        assert [step.operation for step in validated] == expected_operations[ref]
        assert all(item.verifier in manifest.operations and
                   item.operation in manifest.operations[item.verifier] for item in validated)


@pytest.mark.parametrize("step", [
    {"verifier": "http", "operation": "reachable", "target_ref": "production", "route_ref": "home"},
    {"verifier": "http", "operation": "status", "target_ref": "production", "route_ref": "home", "expected": 200},
    {"verifier": "http", "operation": "json_value", "target_ref": "production", "route_ref": "schema",
     "json_path": "auth_sessions_exists", "expected": True},
    {"verifier": "http", "operation": "contains", "target_ref": "production", "route_ref": "home", "expected": "Hello"},
    {"verifier": "http", "operation": "redirect", "target_ref": "production", "route_ref": "login",
     "expected_route_ref": "dashboard"},
    {"verifier": "browser", "operation": "navigate", "target_ref": "production", "route_ref": "login"},
    {"verifier": "browser", "operation": "fill", "target_ref": "production", "selector_ref": "password",
     "credential_ref": "valid_test_user.password"},
    {"verifier": "browser", "operation": "click", "target_ref": "production", "selector_ref": "submit"},
    {"verifier": "browser", "operation": "wait_for", "target_ref": "production", "selector_ref": "dashboard_heading"},
    {"verifier": "browser", "operation": "assert_text", "target_ref": "production",
     "selector_ref": "dashboard_heading", "expected": "Deployment dashboard"},
    {"verifier": "browser", "operation": "assert_url", "target_ref": "production", "route_ref": "dashboard"},
    {"verifier": "browser", "operation": "assert_visible", "target_ref": "production",
     "selector_ref": "dashboard_heading"},
    {"verifier": "shell", "operation": "pytest", "command_ref": "regression"},
    {"verifier": "shell", "operation": "compileall", "command_ref": "syntax"},
])
def test_planner_accepts_each_manifest_operation_with_valid_symbolic_refs(tmp_path, step):
    context = _planner_context(tmp_path)
    validated = VerificationPlanner._validate_step(StepProposal.model_validate(step),
                                                    context.manifest(), context)
    assert validated.verifier == step["verifier"] and validated.operation == step["operation"]


@pytest.mark.parametrize("step,reason", [
    ({"verifier": "shell", "operation": "exec", "command_ref": "regression"}, "unsupported_operation"),
    ({"verifier": "http", "operation": "post", "target_ref": "production", "route_ref": "home"}, "unsupported_operation"),
    ({"verifier": "http", "operation": "status", "target_ref": "internal", "route_ref": "home", "expected": 200}, "invalid_target_ref"),
    ({"verifier": "http", "operation": "status", "target_ref": "production", "expected": 200}, "missing_route_ref"),
    ({"verifier": "http", "operation": "status", "target_ref": "production", "route_ref": "internal", "expected": 200}, "invalid_route_ref"),
    ({"verifier": "browser", "operation": "click", "target_ref": "production", "selector_ref": "internal"}, "invalid_selector_ref"),
    ({"verifier": "shell", "operation": "pytest", "command_ref": "internal"}, "invalid_command_ref"),
    ({"verifier": "shell", "operation": "pytest", "command_ref": "regression", "target_ref": "production"}, "invalid_parameter_combination"),
    ({"verifier": "browser", "operation": "fill", "target_ref": "production", "selector_ref": "password",
      "credential_ref": "valid_test_user.password", "route_ref": "login"}, "invalid_parameter_combination"),
    ({"verifier": "http", "operation": "status", "target_ref": "production", "route_ref": "home", "expected": "200"}, "unsupported_expected_value"),
])
def test_planner_rejects_unsupported_steps_with_specific_safe_reason(tmp_path, step, reason):
    context = _planner_context(tmp_path)
    with pytest.raises(PlanningError) as error:
        VerificationPlanner._validate_step(StepProposal.model_validate(step), context.manifest(), context,
            condition_id="AUTH-PROD-07", acceptance_ref="auth", step_index=2)
    assert error.value.code == "unsupported_step"
    details = json.loads(str(error.value).partition(": ")[2])
    assert details["reason"] == reason
    assert details["condition_id"] == "AUTH-PROD-07"
    assert details["acceptance_ref"] == "auth" and details["step_index"] == 2
    assert details["verifier"] == step["verifier"] and details["operation"] == step["operation"]
    for field, allowed in (("target_ref", context.manifest().target_refs),
                           ("route_ref", context.manifest().route_refs),
                           ("selector_ref", context.manifest().selector_refs),
                           ("command_ref", context.manifest().command_refs)):
        value = step.get(field)
        assert details[field] == (value if value in allowed else "<untrusted>" if value is not None else None)
    assert "fake-test-password" not in str(error.value)


def test_planner_diagnostic_redacts_non_symbolic_model_text(tmp_path):
    context = _planner_context(tmp_path)
    simulated_secret = "simulated-sensitive-value"
    step = StepProposal(verifier="http", operation="status", target_ref=simulated_secret,
                        route_ref="home", credential_ref=simulated_secret, expected=200)
    with pytest.raises(PlanningError) as error:
        VerificationPlanner._validate_step(step, context.manifest(), context,
            condition_id="HOME_9", acceptance_ref="home", step_index=0)
    assert simulated_secret not in str(error.value)
    assert json.loads(str(error.value).partition(": ")[2])["target_ref"] == "<untrusted>"


def test_invalid_scenario_plan_reports_dynamic_condition_and_stops_before_verification(tmp_path):
    conditions = deepcopy(demo_v07._DEFAULT_CONTRACT["conditions"])
    conditions[0]["id"] = "PUBLIC_73"
    conditions[-1]["prerequisites"] = [conditions[2]["id"]]
    steps = {ref: {"steps": actions} for ref, actions in REQUIRED_STEPS.items()}
    steps["home"] = {"steps": [{"verifier": "http", "operation": "status",
                                 "target_ref": "http://169.254.169.254/private", "route_ref": "home",
                                 "expected": 200}]}
    model = OfflineFullLoopModel(contract=_contract_with(conditions), steps=steps)
    pack_root = tmp_path / "packs"
    with pytest.raises(PlanningError) as error:
        run_demo(output_root=pack_root, offline_model=model)
    assert error.value.stage == "planning" and error.value.code == "unsupported_step"
    details = json.loads(str(error.value).partition(": ")[2])
    assert details == {"reason": "invalid_target_ref", "condition_id": "PUBLIC_73",
                       "acceptance_ref": "home", "step_index": 0, "verifier": "http",
                       "operation": "status", "target_ref": "<untrusted>", "route_ref": "home",
                       "selector_ref": None, "command_ref": None}
    assert "169.254" not in str(error.value) and "fake-test-password" not in str(error.value)
    assert model.calls == ["patch", "compiler", "planner:home"]
    assert not list(pack_root.glob("*.json"))


def test_more_than_five_conditions_stops_before_planner(tmp_path):
    base = deepcopy(demo_v07._DEFAULT_CONTRACT["conditions"])
    base.append({"id": "EXTRA_6", "acceptance_ref": "extra", "description": "An extra invented condition",
                 "critical": True, "prerequisites": []})
    model = OfflineFullLoopModel(contract=_contract_with(base))
    with pytest.raises(PlanningError) as error:
        run_demo(output_root=tmp_path / "packs", offline_model=model)
    assert error.value.code == "excessive_conditions"
    assert model.calls == ["patch", "compiler"]


@pytest.mark.parametrize("bad_ref", ["home", "auth", "regression"])
def test_required_schema_probe_cannot_be_replaced_by_other_check(tmp_path, bad_ref):
    steps = {ref: {"steps": actions} for ref, actions in REQUIRED_STEPS.items()}
    steps["schema"] = {"steps": REQUIRED_STEPS[bad_ref]}
    model = OfflineFullLoopModel(steps=steps)
    with pytest.raises(PlanningError) as error:
        run_demo(output_root=tmp_path / "packs", offline_model=model)
    assert error.value.code == "insufficient_coverage"
    assert model.calls == ["patch", "compiler", "planner:home", "planner:schema",
                           "planner:auth", "planner:regression", "planner:dashboard"]
    assert not list((tmp_path / "packs").glob("*.json"))


def test_home_only_plan_fails_coverage_without_verifying(tmp_path):
    home = {"steps": REQUIRED_STEPS["home"]}
    model = OfflineFullLoopModel(steps={ref: home for ref in REQUIRED_STEPS})
    with pytest.raises(PlanningError) as error:
        run_demo(output_root=tmp_path / "packs", offline_model=model)
    assert error.value.code == "insufficient_coverage"
    assert not list((tmp_path / "packs").glob("*.json"))


def test_model_supplied_completion_claim_is_rejected(tmp_path):
    patch = {**demo_v07._DEFAULT_PATCH, "worker_claim": "COMPLETED"}
    model = OfflineFullLoopModel(patch=patch)
    with pytest.raises(WorkerDecisionError, match="invalid_model_action"):
        run_demo(output_root=tmp_path / "packs", offline_model=model)
    assert model.calls == ["patch"]


def test_model_selected_bad_patch_blocks_compilation(tmp_path):
    patch = {**demo_v07._DEFAULT_PATCH, "replacement_expression": "candidate != stored"}
    model = OfflineFullLoopModel(patch=patch)
    with pytest.raises(RuntimeError, match="did not pass real local tests"):
        run_demo(output_root=tmp_path / "packs", offline_model=model)
    assert model.calls == ["patch"]


def test_wrong_migration_fails_with_first_pack_preserved(tmp_path):
    model = OfflineFullLoopModel(repair={"action": "apply_authorized_migration", "migration_ref": "drop_users"})
    root = tmp_path / "packs"
    with pytest.raises(WorkerDecisionError, match="invalid_model_action"):
        run_demo(output_root=root, offline_model=model)
    packs = list(root.glob("*.json"))
    assert len(packs) == 1 and json.loads(packs[0].read_text())["verdict"] == "FAILED"
    assert len(model.calls) == 8


def test_stale_and_mutated_evidence_rejected(complete_demo, tmp_path):
    result = complete_demo
    context = _context_from_pack(result)
    store = ProofPackStore(result.first_path.parent)
    with pytest.raises(PlanningError) as stale:
        repair_request_from_evidence(result.first, result.prepared, context, store,
            "different-run", result.worker_first.task.task_id, result.plan_fingerprint)
    assert stale.value.code == "stale_or_invalid_pack"
    forged = deepcopy(result.first)
    schema_id = next(item.id for item in result.prepared.contract.conditions if item.acceptance_ref == "schema")
    forged.evidence = [replace(item, evidence_id="forged") if item.condition_id == schema_id else item
                       for item in forged.evidence]
    with pytest.raises(PlanningError) as mismatch:
        repair_request_from_evidence(forged, result.prepared, context, store,
            result.first.run_id, result.worker_first.task.task_id, result.plan_fingerprint)
    assert mismatch.value.code == "pack_mismatch"


def _context_from_pack(result):
    # The workspace is cleaned after the shared integration run; TrustedContext only
    # resolves configured paths and does not execute anything during evidence checks.
    from app.core.intelligence import TrustedContext
    base = next(item.metadata["url"].removesuffix("/health/schema") for item in result.first.evidence
                if item.operation == "json_value")
    return TrustedContext(production_base_url=base, workspace=str(result.workspace_path),
        routes={"home": "/", "login": "/login", "dashboard": "/dashboard", "schema": "/health/schema"},
        selectors={"password": "#password", "submit": "#submit", "dashboard_heading": "#dashboard-heading"},
        commands={"regression": ("pytest", ("-q",))},
        credentials={"valid_test_user.password": "fake-test-password"})


@pytest.mark.parametrize("mutation", ["absent", "blocked", "inconclusive", "wrong_target"])
def test_repair_requires_exact_failed_schema_observation(complete_demo, tmp_path, mutation):
    result = complete_demo
    forged = deepcopy(result.first)
    schema_id = next(item.id for item in result.prepared.contract.conditions if item.acceptance_ref == "schema")
    if mutation == "absent":
        forged.evidence = [item for item in forged.evidence if item.condition_id != schema_id]
    else:
        def mutate(item):
            if item.condition_id != schema_id:
                return item
            if mutation == "wrong_target":
                return replace(item, metadata={**item.metadata, "url": "http://127.0.0.1:1/health/schema"})
            return replace(item, status="BLOCKED" if mutation == "blocked" else "INCONCLUSIVE")
        forged.evidence = [mutate(item) for item in forged.evidence]
    store = ProofPackStore(tmp_path / mutation)
    store.save(forged)  # trusted persistence check passes; semantic evidence check must still reject
    with pytest.raises(PlanningError) as error:
        repair_request_from_evidence(forged, result.prepared, _context_from_pack(result), store,
            forged.run_id, result.worker_first.task.task_id, result.plan_fingerprint)
    assert error.value.code == "missing_schema_evidence"


def test_request_budget_stops_ninth_call_without_invoking_model():
    class Fake:
        model_id = "offline/budget"
        calls = 0
        def complete_json(self, *_args, **_kwargs):
            self.calls += 1
            return ModelResponse({}, ModelUsage(self.model_id, 1, 2, 0.0, request_count=1, reasoning_tokens=0))
    fake = Fake()
    client = RequestBudgetClient(fake, MAX_MODEL_REQUESTS)
    for _ in range(MAX_MODEL_REQUESTS):
        client.complete_json("system", "user")
    with pytest.raises(ModelError) as error:
        client.complete_json("system", "user")
    assert error.value.code == "request_budget_exhausted"
    assert fake.calls == MAX_MODEL_REQUESTS and client.usage_summary()["completion_tokens"] == 16
    assert client.usage_summary()["provider_request_count"] == 0
    live_accounting = RequestBudgetClient(fake, provider_enabled=True)
    live_accounting.complete_json("system", "user")
    assert live_accounting.usage_summary()["provider_requests_attempted"] == 1
    assert live_accounting.usage_summary()["provider_request_count"] == 1


def test_live_gate_and_missing_key_fail_before_client(monkeypatch, tmp_path):
    monkeypatch.delenv("PROOF_RUN_LIVE_FULL_LOOP", raising=False)
    monkeypatch.setattr(demo_v07, "TokenFactoryClient",
                        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("provider client created")))
    with pytest.raises(ValueError, match="PROOF_RUN_LIVE_FULL_LOOP"):
        run_demo(output_root=tmp_path / "packs", live=True)
    monkeypatch.setenv("PROOF_RUN_LIVE_FULL_LOOP", "1")
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    with pytest.raises(ValueError, match="NEBIUS_API_KEY"):
        run_demo(output_root=tmp_path / "packs", live=True)


def test_live_path_constructs_zero_retry_client_without_network(monkeypatch, tmp_path):
    class Failing:
        model_id = "offline/no-network"
        calls = 0
        def complete_json(self, *_args, **_kwargs):
            self.calls += 1
            raise ModelError("provider_unavailable", "simulated failure")
    fake = Failing()
    constructed = []
    def factory(**kwargs):
        constructed.append(kwargs)
        return fake
    monkeypatch.setenv("PROOF_RUN_LIVE_FULL_LOOP", "1")
    monkeypatch.setenv("NEBIUS_API_KEY", "fake-local-key")
    monkeypatch.setattr(demo_v07, "TokenFactoryClient", factory)
    with pytest.raises(WorkerDecisionError, match="provider_unavailable"):
        run_demo(output_root=tmp_path / "packs", live=True)
    assert constructed == [{"retries": 0}]
    assert fake.calls == 1


def test_live_mode_routes_all_roles_through_one_mocked_client(monkeypatch, tmp_path):
    mock = OfflineFullLoopModel()
    constructed = []
    def factory(**kwargs):
        constructed.append(kwargs)
        return mock
    monkeypatch.setenv("PROOF_RUN_LIVE_FULL_LOOP", "1")
    monkeypatch.setenv("NEBIUS_API_KEY", "fake-local-key")
    monkeypatch.setattr(demo_v07, "TokenFactoryClient", factory)
    result = run_demo(output_root=tmp_path / "packs", live=True)
    assert constructed == [{"retries": 0}]
    assert mock.calls == ["patch", "compiler", "planner:home", "planner:schema",
                          "planner:auth", "planner:regression", "planner:dashboard", "repair"]
    assert result.execution_mode == "live_nebius"
    assert result.first.verdict == "FAILED" and result.second.verdict == "VERIFIED"
    assert result.usage["provider_requests_attempted"] == 8
    assert result.usage["provider_request_count"] == 8
    assert not result.workspace_path.exists()


def test_mocked_live_malformed_repair_keeps_failed_pack_and_cleans_up(monkeypatch, tmp_path):
    class MalformedRepairModel(OfflineFullLoopModel):
        def complete_json(self, system, user, **kwargs):
            if len(self.calls) == 7:
                self.calls.append("repair-attempt")
                raise ModelError("malformed_response", "private raw model content")
            return super().complete_json(system, user, **kwargs)
    mock = MalformedRepairModel()
    roots = []
    original = __import__("tempfile").TemporaryDirectory
    def tracked(*args, **kwargs):
        item = original(*args, **kwargs)
        roots.append(Path(item.name))
        return item
    monkeypatch.setenv("PROOF_RUN_LIVE_FULL_LOOP", "1")
    monkeypatch.setenv("NEBIUS_API_KEY", "fake-local-key")
    monkeypatch.setattr(demo_v07, "TokenFactoryClient", lambda **kwargs: mock)
    monkeypatch.setattr("app.core.agent_verification_loop.tempfile.TemporaryDirectory", tracked)
    pack_root = tmp_path / "packs"
    with pytest.raises(WorkerDecisionError, match="malformed_response") as error:
        run_demo(output_root=pack_root, live=True)
    assert "private raw model content" not in str(error.value)
    packs = list(pack_root.glob("*.json"))
    assert len(packs) == 1
    raw = packs[0].read_text()
    assert json.loads(raw)["verdict"] == "FAILED"
    assert "private raw model content" not in raw and "fake-local-key" not in raw
    assert roots and all(not root.exists() for root in roots)
    assert mock.calls[-1] == "repair-attempt" and len(mock.calls) == 8


def test_generic_v04_compiler_rejects_scenario_only_reference():
    class Fake:
        model_id = "offline/generic"
        def complete_json(self, *_args, **_kwargs):
            return ModelResponse({"conditions": [{"id": "HOME_1", "description": "Public home is reachable",
                "critical": True, "prerequisites": [], "acceptance_ref": "home"}]},
                ModelUsage(self.model_id, 0, 0, 0.0))
    context = TrustedContext(production_base_url="https://example.com", workspace=None,
                             routes={"home": "/"}, selectors={})
    with pytest.raises(PlanningError) as error:
        OutcomeContractCompiler(Fake()).compile("Check public home", context)
    assert error.value.code == "invalid_contract"


def test_provider_failure_is_safe_and_workspace_cleans_up(monkeypatch, tmp_path):
    class Failing:
        model_id = "offline/failure"
        def complete_json(self, *_args, **_kwargs):
            raise ModelError("provider_unavailable", "private-provider-output")
    roots = []
    original = __import__("tempfile").TemporaryDirectory
    def tracked(*args, **kwargs):
        item = original(*args, **kwargs)
        roots.append(Path(item.name))
        return item
    monkeypatch.setattr("app.core.agent_verification_loop.tempfile.TemporaryDirectory", tracked)
    with pytest.raises(WorkerDecisionError, match="provider_unavailable") as error:
        AgentVerificationLoop(Failing(), "offline_mock", tmp_path / "packs").run()
    assert "private-provider-output" not in str(error.value)
    assert roots and all(not root.exists() for root in roots)


def test_packs_hold_provenance_without_secrets_or_raw_prompts(complete_demo):
    result = complete_demo
    for path in (result.first_path, result.second_path):
        raw = path.read_text()
        for forbidden in ("fake-test-password", "NEBIUS_API_KEY", "Authorization", "output_excerpt",
                          "reasoning_content", "private-provider-output"):
            assert forbidden not in raw
        pack = json.loads(raw)
        assert pack["contract_id"] == result.prepared.contract.contract_id
        assert pack["intelligence"]["plan_fingerprint"] == result.plan_fingerprint
        assert pack["intelligence"]["contract"]["provenance"]["prompt_version"]
        assert pack["intelligence"]["plan_provenance"]["prompt_version"]
        assert pack["intelligence"]["model_execution"] == "offline_mock"
