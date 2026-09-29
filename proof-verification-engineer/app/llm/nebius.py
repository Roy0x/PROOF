from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from app.core.contracts import Condition, OutcomeContract
from app.core.planner import default_contract
from .schemas import ALLOWED_DEMO_METHODS


def _extract_json(text: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(re.search(r"\{.*\}", text, re.DOTALL).group(0))  # type: ignore[union-attr]


def generate_contract_with_nebius(goal: str) -> OutcomeContract:
    """Optionally obtain a contract, rejecting anything outside deterministic methods."""
    api_key = os.getenv("NEBIUS_API_KEY")
    if not api_key:
        return default_contract(goal)
    model = os.getenv("NEBIUS_MODEL", "nvidia/Nemotron-3_5-Lightning")
    system = "Return strict JSON outcome contracts. Only use: " + ", ".join(sorted(ALLOWED_DEMO_METHODS))
    try:
        response = httpx.post(os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1").rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": f"Goal: {goal}"}], "temperature": 0.1}, timeout=30.0)
        response.raise_for_status()
        data = _extract_json(response.json()["choices"][0]["message"]["content"] or "")
        conditions = [Condition(str(item.get("id", f"c{i}")), str(item.get("name", "Verification condition")), str(item.get("description", "")), bool(item.get("critical", True)), item["method"], dict(item.get("params") or {}))
                      for i, item in enumerate(data.get("conditions", []), 1) if item.get("method") in ALLOWED_DEMO_METHODS]
        if len(conditions) < 5:
            return default_contract(goal)
        return OutcomeContract(str(data.get("goal") or goal), str(data.get("summary") or "Independent verification contract"), conditions, f"nebius:{model}")
    except Exception:
        return default_contract(goal)
