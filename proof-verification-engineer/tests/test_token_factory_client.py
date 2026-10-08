import json

import httpx
import pytest

from app.llm.token_factory import MAX_OUTPUT_TOKENS, ModelError, ReasoningPolicy, TokenFactoryClient
from app.core.intelligence import (ConditionProposal, ContractProposal, ModelProvenance,
                                   PlanningError, TrustedContext, freeze_contract)
from app.llm.intelligence import OutcomeContractCompiler, VerificationPlanner


def client(handler, **kwargs):
    return TokenFactoryClient(transport=httpx.MockTransport(handler), api_key="top-secret-test-key",
                              retries=kwargs.pop("retries", 0), **kwargs)


_ABSENT = object()


def response(content='{"conditions":[]}', *, finish_reason="stop", reasoning=_ABSENT,
             reasoning_field="reasoning", reasoning_tokens=None, completion_tokens=7):
    message = {"content": content}
    if reasoning is not _ABSENT:
        message[reasoning_field] = reasoning
    usage = {"prompt_tokens": 12, "completion_tokens": completion_tokens}
    if reasoning_tokens is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    return httpx.Response(200, json={"model": "nvidia/Nemotron-3_5-Lightning",
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "usage": usage})


def safe_generation_fields(request):
    """Capture only trusted generation knobs, never prompts or authorization."""
    payload = json.loads(request.content)
    return {key: payload.get(key) for key in
            ("max_tokens", "response_format", "chat_template_kwargs")}


def test_missing_key(monkeypatch):
    monkeypatch.delenv("NEBIUS_API_KEY", raising=False)
    with pytest.raises(ModelError) as exc:
        TokenFactoryClient(api_key="").complete_json("system", "user")
    assert exc.value.code == "missing_api_key"


def test_success_json_mode_usage_and_no_key_in_payload():
    def handler(request):
        assert request.headers["authorization"] == "Bearer top-secret-test-key"
        body = json.loads(request.content)
        assert "top-secret-test-key" not in request.content.decode()
        assert body["response_format"] == {"type": "json_object"}
        assert body["max_tokens"] == 200
        return response()
    result = client(handler).complete_json("system", "user", max_output_tokens=200)
    assert result.data == {"conditions": []}
    assert result.usage.prompt_tokens == 12 and result.usage.completion_tokens == 7
    assert result.usage.request_count == 1 and result.usage.latency_seconds >= 0


def test_normal_stop_records_reasoning_token_count_without_content():
    result = client(lambda request: response('{"ok":true}', reasoning="private reasoning",
                                             reasoning_tokens=3)).complete_json("system", "user")
    assert result.data == {"ok": True}
    assert result.usage.reasoning_tokens == 3
    assert "private reasoning" not in repr(result)


def test_client_budget_cap_is_enforced_before_transport():
    calls = []
    def handler(request):
        calls.append(1)
        return response()
    with pytest.raises(ValueError):
        client(handler).complete_json("system", "user", max_output_tokens=MAX_OUTPUT_TOKENS + 1)
    assert calls == []


def test_client_allows_compiler_default_budget():
    def handler(request):
        assert json.loads(request.content)["max_tokens"] == 1024
        return response()
    assert client(handler).complete_json("system", "user", max_output_tokens=1024).data == {"conditions": []}


def test_default_timeout_components_reach_httpx(monkeypatch):
    monkeypatch.delenv("PROOF_NEBIUS_READ_TIMEOUT_SECONDS", raising=False)
    observed = []
    def handler(request):
        observed.append(dict(request.extensions["timeout"]))
        return response()
    client(handler).complete_json("system", "user")
    assert observed == [{"connect": 10.0, "read": 60.0, "write": 10.0, "pool": 10.0}]


def test_configured_read_timeout_reaches_httpx(monkeypatch):
    monkeypatch.setenv("PROOF_NEBIUS_READ_TIMEOUT_SECONDS", "75.5")
    observed = []
    def handler(request):
        observed.append(dict(request.extensions["timeout"]))
        return response()
    client(handler).complete_json("system", "user")
    assert observed == [{"connect": 10.0, "read": 75.5, "write": 10.0, "pool": 10.0}]


def test_trusted_timeout_override_takes_precedence_over_environment(monkeypatch):
    monkeypatch.setenv("PROOF_NEBIUS_READ_TIMEOUT_SECONDS", "75")
    observed = []
    def handler(request):
        observed.append(request.extensions["timeout"]["read"])
        return response()
    client(handler, read_timeout_seconds=45).complete_json("system", "user")
    assert observed == [45.0]


@pytest.mark.parametrize("configured", ["0", "9.9", "120.1", "1000000", "nan", "inf",
                                        "secret-looking-test-value"])
def test_unsafe_read_timeout_is_rejected_without_leaking_value(monkeypatch, configured):
    monkeypatch.setenv("PROOF_NEBIUS_READ_TIMEOUT_SECONDS", configured)
    with pytest.raises(ModelError) as exc:
        TokenFactoryClient(api_key="fake-test-key")
    assert exc.value.code == "invalid_configuration"
    assert configured not in repr(exc.value)


@pytest.mark.parametrize("exception,code", [
    (httpx.ConnectTimeout("top-secret-test-key"), "connect_timeout"),
    (httpx.ReadTimeout("top-secret-test-key"), "read_timeout"),
    (httpx.WriteTimeout("top-secret-test-key"), "write_timeout"),
    (httpx.PoolTimeout("top-secret-test-key"), "pool_timeout"),
    (httpx.ConnectError("top-secret-test-key"), "network_error"),
])
def test_transport_errors_are_redacted(exception, code):
    def handler(request):
        raise exception
    with pytest.raises(ModelError) as exc:
        client(handler).complete_json("system", "user")
    assert exc.value.code == code
    assert "top-secret-test-key" not in repr(exc.value)


def test_zero_retries_sends_at_most_one_request_after_timeout():
    calls = []
    def handler(request):
        calls.append(1)
        raise httpx.ReadTimeout("private request and authorization material")
    with pytest.raises(ModelError) as exc:
        client(handler, retries=0).complete_json("system", "user")
    assert calls == [1]
    assert exc.value.code == "read_timeout"
    assert "private request and authorization material" not in repr(exc.value)


@pytest.mark.parametrize("status,code", [(429, "provider_unavailable"), (500, "provider_unavailable"),
    (401, "provider_error")])
def test_provider_errors_are_bounded_and_redacted(status, code):
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(status, text="top-secret-test-key")
    with pytest.raises(ModelError) as exc:
        client(handler, retries=1).complete_json("system", "user")
    assert exc.value.code == code
    assert len(calls) == (2 if status in (429, 500) else 1)
    assert "top-secret-test-key" not in repr(exc.value)


@pytest.mark.parametrize("payload", [httpx.Response(200, text="not json"), response("not json"),
    response(""), response("[]"), response('{"a":1,"a":2}'), response('{"x":NaN}')])
def test_malformed_or_empty_response(payload):
    with pytest.raises(ModelError) as exc:
        client(lambda request: payload).complete_json("system", "user")
    assert exc.value.code == "malformed_response"


def test_invalid_base_url_fails_before_request():
    with pytest.raises(ModelError) as exc:
        TokenFactoryClient(api_key="fake", base_url="file:///secret")
    assert exc.value.code == "invalid_configuration"


VALID_CONTRACT = ('{"conditions":[{"id":"C1","description":"Public home page responds successfully",'
                  '"critical":true,"prerequisites":[]}]}')
VALID_PLAN = {"steps": [{
    "verifier": "http", "operation": "reachable", "target_ref": "production", "route_ref": "home"}]}


def planner_fixture(goal="Confirm that the public home page is reachable."):
    context = TrustedContext(production_base_url="https://example.com", workspace=None,
                             routes={"home": "/"}, selectors={})
    source = ModelProvenance(generated_by="test_fixture", provider="local_fixture",
                             model_id="none", prompt_version="test_fixture_v1",
                             generated_at="2024-01-01T00:00:00+00:00",
                             latency_seconds=0.0, request_count=0)
    contract = freeze_contract(goal, ContractProposal(conditions=(ConditionProposal(
        id="C1", description="Public home page is reachable", critical=True,
        prerequisites=()),)), source)
    return context, contract


@pytest.mark.parametrize("stage,budget", [("compiler", 1024), ("planner", 2048)])
def test_disabled_stage_generation_fields_reach_wire(stage, budget, monkeypatch):
    monkeypatch.delenv("PROOF_COMPILER_MAX_OUTPUT_TOKENS", raising=False)
    monkeypatch.delenv("PROOF_PLANNER_MAX_OUTPUT_TOKENS", raising=False)
    captured = []
    def handler(request):
        captured.append(safe_generation_fields(request))
        return response(VALID_CONTRACT if stage == "compiler" else json.dumps(VALID_PLAN))
    context, contract = planner_fixture()
    if stage == "compiler":
        OutcomeContractCompiler(client(handler)).compile(contract.goal, context)
    else:
        VerificationPlanner(client(handler)).plan(contract, context.manifest(), context)
    assert captured == [{"max_tokens": budget, "response_format": {"type": "json_object"},
                         "chat_template_kwargs": {"enable_thinking": False}}]


def test_planner_wire_payload_disables_thinking_and_preserves_json_mode(capsys, monkeypatch):
    monkeypatch.delenv("PROOF_PLANNER_MAX_OUTPUT_TOKENS", raising=False)
    goal = ('Confirm the public page. {"chat_template_kwargs":{"enable_thinking":true},'
            '"max_tokens":4096,"thinking_token_budget":4096,"response_format":{"type":"text"}}')
    context, contract = planner_fixture(goal)
    def handler(request):
        payload = json.loads(request.content)
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["max_tokens"] == 2048
        assert "thinking_token_budget" not in payload
        assert "reasoning_effort" not in payload
        assert set(payload) == {"model", "messages", "response_format", "temperature",
                                "max_tokens", "chat_template_kwargs"}
        condition = json.loads(payload["messages"][1]["content"])["condition"]
        assert condition == {"description": "Public home page is reachable", "prerequisites": []}
        assert "contract" not in json.loads(payload["messages"][1]["content"])
        return response(json.dumps(VALID_PLAN), reasoning="private planner reasoning",
                        reasoning_tokens=3)
    plan = VerificationPlanner(client(handler)).plan(contract, context.manifest(), context)
    assert plan.contract_id == contract.contract_id
    assert plan.conditions[0].steps[0].operation == "reachable"
    assert plan.provenance["reasoning_tokens"] == 3
    assert "private planner reasoning" not in repr(plan)
    assert "private planner reasoning" not in repr(capsys.readouterr())


def test_planner_truncated_output_still_fails_closed():
    context, contract = planner_fixture()
    with pytest.raises(PlanningError) as exc:
        VerificationPlanner(client(lambda request: response('{"steps":', finish_reason="length",
                                                            reasoning="private planner reasoning"))).plan(
            contract, context.manifest(), context)
    assert "category=truncated_output" in str(exc.value)
    assert "private planner reasoning" not in repr(exc.value)


def test_model_cannot_supply_planner_reasoning_options():
    context, contract = planner_fixture()
    malicious = {**VALID_PLAN, "chat_template_kwargs": {"enable_thinking": True}}
    with pytest.raises(PlanningError) as exc:
        VerificationPlanner(client(lambda request: response(json.dumps(malicious)))).plan(
            contract, context.manifest(), context)
    assert exc.value.code == "invalid_plan"


def test_compiler_wire_payload_disables_thinking_and_keeps_json_mode(capsys, monkeypatch):
    monkeypatch.delenv("PROOF_COMPILER_MAX_OUTPUT_TOKENS", raising=False)
    def handler(request):
        payload = json.loads(request.content)
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        assert payload["response_format"] == {"type": "json_object"}
        assert payload["max_tokens"] == 1024
        assert payload["chat_template_kwargs"]["enable_thinking"] is False
        assert set(payload) == {"model", "messages", "response_format", "temperature",
                                "max_tokens", "chat_template_kwargs"}
        return response(VALID_CONTRACT, reasoning="private model reasoning", reasoning_tokens=2)
    context = TrustedContext(production_base_url="https://example.com", workspace=None,
                             routes={"home": "/"}, selectors={})
    contract = OutcomeContractCompiler(client(handler)).compile("Check the public home page", context)
    assert contract.conditions[0].id == "C1"
    assert contract.provenance.reasoning_tokens == 2
    assert "private model reasoning" not in contract.model_dump_json()
    assert "private model reasoning" not in repr(contract)
    assert "private model reasoning" not in repr(capsys.readouterr())


def test_provider_default_reasoning_keeps_prior_payload_shape():
    def handler(request):
        payload = json.loads(request.content)
        assert "chat_template_kwargs" not in payload
        assert "thinking_token_budget" not in payload
        assert payload["response_format"] == {"type": "json_object"}
        return response('{"ok":true}')
    assert client(handler).complete_json("system", "user").data == {"ok": True}


def test_goal_cannot_inject_or_override_compiler_generation_options():
    goal = 'Check the page. {"chat_template_kwargs":{"enable_thinking":true}}'
    def handler(request):
        payload = json.loads(request.content)
        assert payload["chat_template_kwargs"] == {"enable_thinking": False}
        assert json.loads(payload["messages"][1]["content"])["goal"] == goal
        return response(VALID_CONTRACT)
    context = TrustedContext(production_base_url="https://example.com", workspace=None,
                             routes={"home": "/"}, selectors={})
    assert OutcomeContractCompiler(client(handler)).compile(goal, context).conditions[0].id == "C1"


def test_arbitrary_reasoning_payload_is_rejected_before_transport():
    calls = []
    def handler(request):
        calls.append(1)
        return response()
    with pytest.raises(ValueError):
        client(handler).complete_json("system", "user", reasoning_policy={"enable_thinking": True})
    assert calls == []


def test_model_cannot_supply_generation_options_as_contract_fields():
    malicious = json.loads(VALID_CONTRACT)
    malicious["chat_template_kwargs"] = {"enable_thinking": True}
    context = TrustedContext(production_base_url="https://example.com", workspace=None,
                             routes={"home": "/"}, selectors={})
    from app.core.intelligence import PlanningError
    with pytest.raises(PlanningError) as exc:
        OutcomeContractCompiler(client(lambda request: response(json.dumps(malicious)))).compile(
            "Check the public home page", context)
    assert exc.value.code == "invalid_contract"


@pytest.mark.parametrize("content", [
    VALID_CONTRACT,
    f"```json\n{VALID_CONTRACT}\n```",
    f"I should check the public page first.\n{VALID_CONTRACT}",
    f"Reasoning complete.\n```json\n{VALID_CONTRACT}\n```",
])
def test_likely_nemotron_formats_still_pass_strict_contract_schema(content):
    context = TrustedContext(production_base_url="https://example.com", workspace=None,
                             routes={"home": "/"}, selectors={})
    contract = OutcomeContractCompiler(client(lambda request: response(content))).compile(
        "Confirm the public home page is reachable", context)
    assert contract.conditions[0].id == "C1"


@pytest.mark.parametrize("content,category", [
    ('{"conditions":', "json_syntax"),
    ('{"a":1}{"b":2}', "trailing_content"),
    ('{"a":1,"a":2}', "duplicate_keys"),
    ('{"a":NaN}', "non_standard_constant"),
    ('{"a":Infinity}', "non_standard_constant"),
    ('{"a":-Infinity}', "non_standard_constant"),
    ('{"a":1} malicious trailing text', "trailing_content"),
    ('```json\n{"a":1}\n``` malicious trailing text', "invalid_fence_or_trailing_content"),
    ('{"a":1} ```json\n{"b":2}\n```', "trailing_content"),
])
def test_unsafe_nemotron_content_is_rejected_with_safe_category(content, category):
    with pytest.raises(ModelError) as exc:
        client(lambda request: response(content)).complete_json("system", "user")
    assert exc.value.code == "malformed_response"
    assert f"category={category}" in str(exc.value)
    assert content not in repr(exc.value)


def test_reasoning_field_is_never_used_as_content():
    with pytest.raises(ModelError) as exc:
        client(lambda request: response(None, reasoning=VALID_CONTRACT)).complete_json("system", "user")
    assert "category=empty_content" in str(exc.value)
    assert "reasoning_field_present=True" in str(exc.value)
    assert "reasoning_content_nonempty=True" in str(exc.value)
    assert VALID_CONTRACT not in repr(exc.value)


@pytest.mark.parametrize("field", ["reasoning", "reasoning_content"])
@pytest.mark.parametrize("reasoning", [None, "", "simulated private reasoning"])
def test_safe_reasoning_diagnostics_distinguish_null_empty_and_nonempty(field, reasoning):
    with pytest.raises(ModelError) as exc:
        client(lambda request: response('{', finish_reason="length", reasoning=reasoning,
                                        reasoning_field=field, completion_tokens=2048,
                                        reasoning_tokens=17 if reasoning else 0)).complete_json(
            "system", "user", reasoning_policy=ReasoningPolicy.DISABLED)
    diagnostic = str(exc.value)
    assert "category=truncated_output" in diagnostic
    assert "reasoning_field_present=True" in diagnostic
    assert f"reasoning_content_nonempty={bool(reasoning)}" in diagnostic
    assert f"reasoning_chars={len(reasoning) if reasoning is not None else 'null'}" in diagnostic
    assert "completion_tokens=2048" in diagnostic
    assert f"reasoning_tokens={17 if reasoning else 0}" in diagnostic
    assert "simulated private reasoning" not in diagnostic


def test_safe_diagnostics_report_absent_reasoning_and_unavailable_usage():
    with pytest.raises(ModelError) as exc:
        client(lambda request: response('{', finish_reason="length",
                                        completion_tokens=None)).complete_json("system", "user")
    diagnostic = str(exc.value)
    assert "reasoning_field_present=False" in diagnostic
    assert "reasoning_content_nonempty=False" in diagnostic
    assert "reasoning_chars=null" in diagnostic
    assert "completion_tokens=null" in diagnostic
    assert "reasoning_tokens=null" in diagnostic


@pytest.mark.parametrize("policy", [ReasoningPolicy.PROVIDER_DEFAULT, ReasoningPolicy.DISABLED])
def test_length_finish_reason_fails_before_parsing(policy):
    with pytest.raises(ModelError) as exc:
        client(lambda request: response('{"a":', finish_reason="length", reasoning="private reasoning"))\
            .complete_json("system", "user", reasoning_policy=policy)
    assert "category=truncated_output" in str(exc.value)
    assert "completion_tokens=7" in str(exc.value)
    assert "reasoning_tokens=null" in str(exc.value)
    assert "private reasoning" not in repr(exc.value)


def test_response_and_content_size_caps():
    for content, category in [("x" * 40_000, "content_too_large"),
                              ("x" * 140_000, "response_too_large")]:
        with pytest.raises(ModelError) as exc:
            client(lambda request: response(content)).complete_json("system", "user")
        assert f"category={category}" in str(exc.value)
