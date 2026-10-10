"""Two independent Nemotron stages; neither stage evaluates evidence."""
from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import urljoin

from pydantic import Field, ValidationError

from app.core.intelligence import (CapabilityManifest, ContractProposal, FrozenOutcomeContract,
                                   PlanningError, StrictModel, TrustedContext, freeze_contract, provenance)
from app.verifiers.models import PlannedCondition, VerificationPlan, VerificationStep
from .token_factory import ModelClient, ModelError, ModelUsage, ReasoningPolicy, validate_output_budget

COMPILER_PROMPT_VERSION = "contract_compiler_v1"
PLANNER_PROMPT_VERSION = "verification_planner_v5"
SCENARIO_COMPILER_PROMPT_VERSION = "contract_compiler_v3_acceptance_prerequisites"
SCENARIO_PLANNER_PROMPT_VERSION = "verification_planner_v7_capability_fields"
DEFAULT_COMPILER_OUTPUT_TOKENS = 1024
DEFAULT_PLANNER_OUTPUT_TOKENS = 2048


def _stage_output_budget(override: int | None, env_name: str, default: int) -> int:
    if override is not None:
        return validate_output_budget(override)
    configured = os.getenv(env_name)
    if configured is None:
        return default
    if not configured.isascii() or not configured.isdecimal():
        raise ValueError(f"{env_name} must be a bounded positive integer")
    try:
        return validate_output_budget(int(configured))
    except ValueError:
        raise ValueError(f"{env_name} must be a bounded positive integer") from None

COMPILER_SYSTEM = """You are PROOF's outcome contract compiler. Return one JSON object with exactly a conditions array.
Each condition has id, description, critical (JSON boolean), prerequisites (array of earlier condition IDs).
Conditions must be observable, testable, specific, outcome-oriented, independent from worker claims,
minimally sufficient, and non-duplicative. Prefer user-visible outcomes over implementation details.
Never claim the outcome succeeded. Do not include contract ID, version, timestamps, methods, or verdict."""

SCENARIO_COMPILER_SYSTEM = COMPILER_SYSTEM + """
Trusted acceptance requirements are supplied separately from the goal. Return EXACTLY ONE observable,
critical condition for EACH requirement. Each condition must include acceptance_ref copied verbatim
from that requirement's ref, plus your own unique id, description, critical=true, and prerequisites.
You may choose condition IDs and order, subject to these EXACT prerequisite rules:
- acceptance_ref home, auth, schema, and regression are independent checks. Their prerequisites MUST be [].
  Do not make authentication depend on schema or home, or make schema or regression depend on anything.
- acceptance_ref dashboard MUST have prerequisites containing ONLY the id you generated for the auth
  condition: [auth_condition_id]. Put auth before dashboard. No other prerequisite is allowed.
Copy that auth id verbatim; do not use the acceptance_ref or description as an id. Every prerequisite
must refer to an earlier condition. For example, if auth has id LOGIN_7 and dashboard has id DASH_2,
dashboard prerequisites must be ["LOGIN_7"]; all other prerequisites must be [].
Do not add, omit, rename, or merge acceptance references. Do not include operations, claims,
evidence, or a verdict."""

PLANNER_SYSTEM = """You are PROOF's verification-step planner for ONE selected frozen condition. Return ONE JSON object with exactly a steps array. No reasoning, commentary, or prose outside JSON.
PROOF owns contract identity, frozen condition IDs, condition count and order, prerequisites, and final verdict. You select executable verification steps ONLY. Never output conditions, condition_id, contract_id, verdict, or edits to the frozen condition.
Use the minimum sufficient steps: prefer ONE deterministic step when it directly verifies the condition. Use more than one only when independently necessary, with at most 8 steps. Do not duplicate a verifier/operation/target combination or add redundant HTTP, browser, or shell checks.
Do not restate the goal, condition description, prerequisites, or capability descriptions. Do not invent metadata or explanations. Include only steps and each step's required fields or fields needed to execute it.
Example input condition: {"description":"Public home page is reachable","prerequisites":[]}
Minimal response: {"steps":[{"verifier":"http","operation":"reachable","target_ref":"production","route_ref":"home"}]}
The example is illustrative; use only the supplied capability manifest.
Allowed step fields when needed: verifier, operation, target_ref, route_ref, selector_ref, command_ref, credential_ref, expected, expected_route_ref, json_path.
Use symbolic references from the manifest only. An empty operation list means that verifier is unavailable. For browser fill, use credential_ref, never a secret.
Do not include shell commands, JavaScript, arbitrary URLs, passwords, evidence, or verdicts.
HTTP steps use GET. Shell commands use command_ref."""

SCENARIO_PLANNER_SYSTEM = PLANNER_SYSTEM + """
For this scenario, condition.acceptance_ref selects ONE trusted acceptance outcome and
condition.required_check.steps supplies the exact authorized steps. Return exactly those
steps in the same order. Copy their field names, symbolic refs, and expected values;
do not add optional fields, alternate checks, or extra steps. The capability manifest
must also authorize every operation and reference. The five required field combinations are:
- home: http/status with target_ref=production, route_ref=home, expected=200.
- auth: browser/navigate with target_ref=production and route_ref=login; then browser/fill
  with target_ref=production, selector_ref=password, credential_ref=valid_test_user.password;
  then browser/click with target_ref=production, selector_ref=submit; then browser/wait_for
  with target_ref=production, selector_ref=dashboard_heading.
- dashboard: browser/assert_text with target_ref=production, selector_ref=dashboard_heading,
  expected="Deployment dashboard".
- schema: http/json_value with target_ref=production, route_ref=schema,
  json_path=auth_sessions_exists, expected=true (JSON boolean).
- regression: shell/pytest with command_ref=regression ONLY; omit target_ref, route_ref,
  selector_ref, expected, and shell command text. PROOF resolves the trusted command.
For browser selector actions omit route_ref; for HTTP omit selector_ref and credential_ref.
Use only refs listed in the supplied manifest. Do not emit condition objects, raw URLs,
SQL, explanations, or a verdict."""


class StepProposal(StrictModel):
    verifier: str
    operation: str
    target_ref: str | None = None
    route_ref: str | None = None
    selector_ref: str | None = None
    command_ref: str | None = None
    credential_ref: str | None = None
    expected: Any = None
    expected_route_ref: str | None = None
    json_path: str | None = None


class StepPlanProposal(StrictModel):
    steps: tuple[StepProposal, ...] = Field(min_length=1, max_length=8)


def _model_capabilities(manifest: CapabilityManifest) -> dict[str, Any]:
    """Compact planning view; the full trusted manifest remains authoritative."""
    return {"operations": manifest.operations,
            "target_refs": manifest.target_refs,
            "route_refs": manifest.route_refs,
            "selector_refs": manifest.selector_refs,
            "command_refs": manifest.command_refs,
            "credential_refs": manifest.credential_refs}


def _combined_usage(usages: list[ModelUsage]) -> ModelUsage:
    """Aggregate completed per-condition requests without retaining prompts or responses."""
    model_id = usages[0].model_id
    if any(item.model_id != model_id for item in usages):
        raise PlanningError("planning", "model_mismatch", "Planner responses used different models")
    def total(field: str) -> int | None:
        values = [getattr(item, field) for item in usages]
        return sum(values) if all(value is not None for value in values) else None
    return ModelUsage(model_id, total("prompt_tokens"), total("completion_tokens"),
                      sum(item.latency_seconds for item in usages),
                      sum(item.request_count for item in usages), total("reasoning_tokens"))


def _model_call(client: ModelClient, stage: str, system: str, user: str, tokens: int,
                reasoning_policy: ReasoningPolicy):
    try:
        return client.complete_json(system, user, max_output_tokens=tokens,
                                    reasoning_policy=reasoning_policy)
    except ModelError as exc:
        raise PlanningError(stage, exc.code, str(exc)) from None


class OutcomeContractCompiler:
    def __init__(self, client: ModelClient, *, max_output_tokens: int | None = None) -> None:
        self.client = client
        self.max_output_tokens = _stage_output_budget(max_output_tokens,
            "PROOF_COMPILER_MAX_OUTPUT_TOKENS", DEFAULT_COMPILER_OUTPUT_TOKENS)

    def compile(self, goal: str, context: TrustedContext, *,
                trusted_requirements: tuple[dict[str, str], ...] | None = None) -> FrozenOutcomeContract:
        if not goal or not goal.strip():
            raise PlanningError("compilation", "invalid_goal", "Goal must be nonempty")
        sensitive = [os.getenv("NEBIUS_API_KEY", ""), *context.credentials.values()]
        if any(value and value in goal for value in sensitive):
            raise PlanningError("compilation", "secret_in_goal", "Goal contains a configured secret")
        manifest = context.manifest()
        safe_context = {"goal": goal, "capabilities": manifest.model_dump(mode="json")}
        if trusted_requirements is not None:
            safe_context["trusted_acceptance_requirements"] = trusted_requirements
        response = _model_call(self.client, "compilation",
                               SCENARIO_COMPILER_SYSTEM if trusted_requirements is not None else COMPILER_SYSTEM,
                               json.dumps(safe_context), self.max_output_tokens, ReasoningPolicy.DISABLED)
        try:
            proposal = ContractProposal.model_validate(response.data)
        except ValidationError:
            raise PlanningError("compilation", "invalid_contract", "Model contract failed validation") from None
        if trusted_requirements is None and any(item.acceptance_ref is not None for item in proposal.conditions):
            raise PlanningError("compilation", "invalid_contract", "Unexpected scenario acceptance reference")
        version = SCENARIO_COMPILER_PROMPT_VERSION if trusted_requirements is not None else COMPILER_PROMPT_VERSION
        return freeze_contract(goal, proposal, provenance(response.usage.model_id, version, response.usage))


class VerificationPlanner:
    def __init__(self, client: ModelClient, *, max_output_tokens: int | None = None,
                 reasoning_policy: ReasoningPolicy = ReasoningPolicy.DISABLED) -> None:
        # Nebius has not documented a token-level thinking bound for this hosted model.
        # Do not allow the planner to silently fall back to unbounded provider reasoning.
        if reasoning_policy is not ReasoningPolicy.DISABLED:
            raise ValueError("planner reasoning_policy must be DISABLED")
        self.client = client
        self.reasoning_policy = reasoning_policy
        self.max_output_tokens = _stage_output_budget(max_output_tokens,
            "PROOF_PLANNER_MAX_OUTPUT_TOKENS", DEFAULT_PLANNER_OUTPUT_TOKENS)

    def plan(self, contract: FrozenOutcomeContract, capabilities: CapabilityManifest,
             trusted_context: TrustedContext, *, required_checks: dict[str, dict] | None = None) -> VerificationPlan:
        if capabilities != trusted_context.manifest():
            raise PlanningError("planning", "capability_mismatch", "Capability manifest differs from trusted context")
        planned = []
        usages = []
        descriptions = {item.id: item.description for item in contract.conditions}
        for condition in contract.conditions:
            selected = {"description": condition.description,
                        "prerequisites": [descriptions[dep] for dep in condition.prerequisites]}
            if required_checks is not None:
                selected["acceptance_ref"] = condition.acceptance_ref
                selected["required_check"] = required_checks.get(condition.acceptance_ref)
            user = json.dumps({"condition": selected, "capabilities": _model_capabilities(capabilities)},
                              separators=(",", ":"), sort_keys=True)
            response = _model_call(self.client, "planning",
                                   SCENARIO_PLANNER_SYSTEM if required_checks is not None else PLANNER_SYSTEM, user,
                                   self.max_output_tokens, self.reasoning_policy)
            try:
                proposal = StepPlanProposal.model_validate(response.data)
            except ValidationError:
                raise PlanningError("planning", "invalid_plan", "Model steps failed schema validation") from None
            steps = tuple(self._validate_step(step, capabilities, trusted_context,
                               condition_id=condition.id, acceptance_ref=condition.acceptance_ref,
                               step_index=index) for index, step in enumerate(proposal.steps))
            planned.append(PlannedCondition(condition.id, condition.description, condition.critical,
                                            steps, condition.prerequisites))
            usages.append(response.usage)
        source_usage = _combined_usage(usages)
        source = provenance(source_usage.model_id,
                            SCENARIO_PLANNER_PROMPT_VERSION if required_checks is not None else PLANNER_PROMPT_VERSION,
                            source_usage)
        return VerificationPlan(contract.contract_id, contract.version, tuple(planned), source.model_dump())

    @staticmethod
    def _validate_step(step: StepProposal, manifest: CapabilityManifest, context: TrustedContext, *,
                       condition_id: str | None = None, acceptance_ref: str | None = None,
                       step_index: int | None = None) -> VerificationStep:
        def safe_name(value: str) -> str:
            # Never echo arbitrary model text (including a possible secret) in a diagnostic.
            return value if re.fullmatch(r"[a-z][a-z_]{0,23}", value) else "<redacted>"

        def safe_ref(value: str | None, allowed) -> str | None:
            if value is None:
                return None
            return value if value in allowed else "<untrusted>"

        def reject(reason: str) -> None:
            details = {"reason": reason, "condition_id": condition_id,
                       "acceptance_ref": acceptance_ref, "step_index": step_index,
                       "verifier": safe_name(step.verifier), "operation": safe_name(step.operation),
                       "target_ref": safe_ref(step.target_ref, manifest.target_refs),
                       "route_ref": safe_ref(step.route_ref, manifest.route_refs),
                       "selector_ref": safe_ref(step.selector_ref, manifest.selector_refs),
                       "command_ref": safe_ref(step.command_ref, manifest.command_refs)}
            raise PlanningError("planning", "unsupported_step",
                                "Model step exceeds trusted capabilities: " +
                                json.dumps(details, separators=(",", ":")))

        if step.verifier not in manifest.operations or step.operation not in manifest.operations[step.verifier]:
            reject("unsupported_operation")
        if step.verifier == "shell":
            if any((step.target_ref, step.route_ref, step.selector_ref, step.credential_ref,
                    step.expected_route_ref, step.json_path)) or step.expected is not None:
                reject("invalid_parameter_combination")
            if step.command_ref not in manifest.command_refs or manifest.command_refs[step.command_ref] != step.operation:
                reject("invalid_command_ref")
            operation, args = context.commands[step.command_ref]
            return VerificationStep("shell", operation, 0, params={"args": args})

        if step.target_ref != "production":
            reject("invalid_target_ref")
        if step.command_ref is not None:
            reject("invalid_parameter_combination")
        if step.verifier == "http":
            if step.route_ref not in manifest.route_refs:
                reject("missing_route_ref" if step.route_ref is None else "invalid_route_ref")
            if any((step.selector_ref, step.credential_ref)):
                reject("invalid_parameter_combination")
            route = context.routes[step.route_ref]
            if step.operation == "reachable":
                if any((step.expected is not None, step.expected_route_ref, step.json_path)): reject("invalid_parameter_combination")
            elif step.operation == "status":
                if type(step.expected) is not int or not 100 <= step.expected <= 599:
                    reject("unsupported_expected_value")
                if step.expected_route_ref or step.json_path:
                    reject("invalid_parameter_combination")
            elif step.operation == "contains":
                if not isinstance(step.expected, str) or not 1 <= len(step.expected) <= 200:
                    reject("unsupported_expected_value")
                if step.expected_route_ref or step.json_path:
                    reject("invalid_parameter_combination")
            elif step.operation == "json_value":
                if not step.json_path or not all(part.isidentifier() for part in step.json_path.split(".")):
                    reject("invalid_json_path")
                if step.expected_route_ref:
                    reject("invalid_parameter_combination")
                if isinstance(step.expected, (dict, list)):
                    reject("unsupported_expected_value")
            elif step.operation == "redirect":
                if step.expected is not None or step.json_path:
                    reject("invalid_parameter_combination")
                if step.expected_route_ref not in manifest.route_refs:
                    reject("invalid_route_ref")
            expected = context.routes[step.expected_route_ref] if step.operation == "redirect" else step.expected
            return VerificationStep("http", step.operation, expected, route.lstrip("/"),
                                    {"path": step.json_path} if step.json_path else {})

        if step.verifier == "browser":
            if step.json_path or step.expected_route_ref and step.operation != "assert_url": reject("invalid_parameter_combination")
            if step.operation == "navigate":
                if step.route_ref not in manifest.route_refs:
                    reject("missing_route_ref" if step.route_ref is None else "invalid_route_ref")
                if any((step.selector_ref, step.credential_ref, step.expected is not None, step.expected_route_ref)):
                    reject("invalid_parameter_combination")
                target = urljoin(context.production_base_url + "/", context.routes[step.route_ref].lstrip("/"))
                return VerificationStep("browser", "navigate", target=target)
            if step.operation == "assert_url":
                if step.route_ref not in manifest.route_refs:
                    reject("missing_route_ref" if step.route_ref is None else "invalid_route_ref")
                if any((step.selector_ref, step.credential_ref, step.expected is not None, step.expected_route_ref)):
                    reject("invalid_parameter_combination")
                expected = urljoin(context.production_base_url + "/", context.routes[step.route_ref].lstrip("/"))
                return VerificationStep("browser", "assert_url", expected=expected, params={"exact": True})
            if step.route_ref or step.expected_route_ref:
                reject("invalid_parameter_combination")
            if step.selector_ref not in manifest.selector_refs:
                reject("invalid_selector_ref")
            selector = context.selectors[step.selector_ref]
            if step.operation == "fill":
                if step.credential_ref not in manifest.credential_refs:
                    reject("invalid_credential_ref")
                if step.expected is not None:
                    reject("unsupported_expected_value")
                return VerificationStep("browser", "fill", target=selector,
                                        params={"credential_ref": step.credential_ref})
            if step.credential_ref is not None: reject("invalid_parameter_combination")
            if step.operation == "assert_text":
                if not isinstance(step.expected, str) or not 1 <= len(step.expected) <= 200: reject("unsupported_expected_value")
            elif step.expected is not None:
                reject("unsupported_expected_value")
            return VerificationStep("browser", step.operation, expected=step.expected, target=selector)
        reject("unsupported_operation")
