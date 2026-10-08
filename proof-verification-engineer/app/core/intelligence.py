"""Frozen v0.4 contract and trusted capability boundary."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from app.verifiers.browser import BrowserVerifier
from app.verifiers.http import HTTPVerifier
from app.verifiers.shell import ShellVerifier


class PlanningError(Exception):
    def __init__(self, stage: str, code: str, message: str) -> None:
        self.stage, self.code = stage, code
        super().__init__(message)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConditionProposal(StrictModel):
    id: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    description: str = Field(min_length=5, max_length=300)
    critical: StrictBool
    prerequisites: tuple[str, ...] = ()

    @field_validator("description")
    @classmethod
    def independent_observation(cls, value: str) -> str:
        lowered = value.lower()
        if any(phrase in lowered for phrase in ("worker says", "agent says", "worker claims", "agent claims", "worker successfully")):
            raise ValueError("condition relies on a worker claim")
        return value


class ContractProposal(StrictModel):
    conditions: tuple[ConditionProposal, ...] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def valid_graph(self):
        ids = [item.id for item in self.conditions]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate condition ID")
        prior = set()
        for item in self.conditions:
            if len(item.prerequisites) != len(set(item.prerequisites)) or any(dep not in prior for dep in item.prerequisites):
                raise ValueError("prerequisites must refer to earlier conditions")
            prior.add(item.id)
        return self


class ModelProvenance(StrictModel):
    generated_by: str = "nemotron"
    provider: str = "nebius_token_factory"
    model_id: str
    prompt_version: str
    generated_at: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    latency_seconds: float
    request_count: int


class FrozenOutcomeContract(StrictModel):
    contract_id: str
    version: int = 1
    goal: str
    conditions: tuple[ConditionProposal, ...]
    provenance: ModelProvenance

    @model_validator(mode="after")
    def validate_conditions(self):
        ContractProposal(conditions=self.conditions)
        return self


class CapabilityManifest(StrictModel):
    version: str = "v1"
    operations: dict[str, tuple[str, ...]]
    target_refs: tuple[str, ...]
    route_refs: tuple[str, ...]
    selector_refs: tuple[str, ...]
    command_refs: dict[str, str]
    credential_refs: tuple[str, ...]


class TrustedContext:
    def __init__(self, *, production_base_url: str, workspace: str | None,
                 routes: dict[str, str], selectors: dict[str, str],
                 commands: dict[str, tuple[str, tuple[str, ...]]] | None = None,
                 credentials: dict[str, str] | None = None) -> None:
        parsed = urlparse(production_base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("production_base_url must be an HTTP(S) origin or base path")
        if not routes or any(not name or not self._safe_route(route) for name, route in routes.items()):
            raise ValueError("routes must be trusted relative paths")
        if any(not name or not selector for name, selector in selectors.items()):
            raise ValueError("selectors must be nonempty")
        self.production_base_url = production_base_url.rstrip("/")
        self.workspace = str(Path(workspace).resolve()) if workspace else None
        self.routes = dict(routes)
        self.selectors = dict(selectors)
        self.commands = dict(commands or {})
        self.credentials = dict(credentials or {})
        for operation, args in self.commands.values():
            if operation not in ShellVerifier._OPERATIONS or any(not isinstance(x, str) or x.startswith("-") and x not in ("-q", "-s") or ".." in x or "\\" in x or "/" in x for x in args):
                raise ValueError("unsafe trusted command configuration")

    @staticmethod
    def _safe_route(route: str) -> bool:
        return isinstance(route, str) and route.startswith("/") and not route.startswith("//") and not any(
            part in (".", "..") for part in route.split("/")) and "\\" not in route and "?" not in route and "#" not in route

    def manifest(self) -> CapabilityManifest:
        operations = {"http": tuple(sorted(HTTPVerifier._OPERATIONS)),
                      "browser": tuple(sorted(BrowserVerifier._ACTIONS - {"assert_hidden"})),
                      "shell": tuple(sorted(ShellVerifier._OPERATIONS)) if self.workspace else ()}
        return CapabilityManifest(operations=operations, target_refs=("production",),
                                  route_refs=tuple(self.routes), selector_refs=tuple(self.selectors),
                                  command_refs={name: operation for name, (operation, _) in self.commands.items()} if self.workspace else {},
                                  credential_refs=tuple(self.credentials))


def provenance(model_id: str, prompt_version: str, usage) -> ModelProvenance:
    return ModelProvenance(model_id=model_id, prompt_version=prompt_version,
                           generated_at=datetime.now(timezone.utc).isoformat(),
                           prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens,
                           reasoning_tokens=usage.reasoning_tokens,
                           latency_seconds=usage.latency_seconds, request_count=usage.request_count)


def freeze_contract(goal: str, proposal: ContractProposal, source: ModelProvenance) -> FrozenOutcomeContract:
    return FrozenOutcomeContract(contract_id="contract_" + uuid4().hex, version=1, goal=goal,
                                 conditions=proposal.conditions, provenance=source)
