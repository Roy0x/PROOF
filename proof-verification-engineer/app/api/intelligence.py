"""Explicit v0.4 preparation endpoint. Trusted targets come from server configuration."""
from __future__ import annotations

import json
import os
from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.intelligence import PlanningError, TrustedContext
from app.core.intelligence_workflow import IntelligenceWorkflow
from app.llm.token_factory import ModelError, TokenFactoryClient

router = APIRouter(prefix="/api/v0.4")


class PrepareRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)


def configured_context() -> TrustedContext:
    base_url = os.getenv("PROOF_PRODUCTION_BASE_URL")
    if not base_url:
        raise PlanningError("configuration", "missing_target", "PROOF_PRODUCTION_BASE_URL is required")
    try:
        routes = json.loads(os.getenv("PROOF_ROUTES_JSON", '{"home":"/","login":"/login","dashboard":"/dashboard"}'))
        selectors = json.loads(os.getenv("PROOF_SELECTORS_JSON", '{"email":"input[name=email]","password":"input[name=password]","submit":"button"}'))
        if not isinstance(routes, dict) or not isinstance(selectors, dict):
            raise ValueError("routes and selectors must be objects")
        credentials = {}
        if os.getenv("PROOF_TEST_USERNAME"):
            credentials["valid_test_user.username"] = os.environ["PROOF_TEST_USERNAME"]
        if os.getenv("PROOF_TEST_PASSWORD"):
            credentials["valid_test_user.password"] = os.environ["PROOF_TEST_PASSWORD"]
        workspace = os.getenv("PROOF_WORKSPACE")
        commands = {"regression": ("pytest", ("-q",))} if workspace else {}
        return TrustedContext(production_base_url=base_url, workspace=workspace,
                              routes=routes, selectors=selectors, commands=commands, credentials=credentials)
    except (ValueError, TypeError):
        raise PlanningError("configuration", "invalid_context", "Trusted target configuration is invalid") from None


@router.post("/prepare")
def prepare(request: PrepareRequest) -> dict:
    try:
        context = configured_context()
        workflow = IntelligenceWorkflow(TokenFactoryClient(), context)
        result = workflow.prepare(request.goal)
        return {"contract": result.contract.model_dump(mode="json"), "plan": asdict(result.plan)}
    except ModelError as exc:
        raise HTTPException(status_code=503, detail={"stage": "configuration", "code": exc.code,
                                                     "message": str(exc)}) from None
    except PlanningError as exc:
        unavailable = exc.code in ("missing_target", "missing_api_key", "timeout", "network_error", "provider_unavailable")
        raise HTTPException(status_code=503 if unavailable else 422,
                            detail={"stage": exc.stage, "code": exc.code, "message": str(exc)}) from None
