from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any

import httpx


@dataclass
class Condition:
    id: str
    name: str
    description: str
    critical: bool
    method: str
    params: dict[str, Any]


@dataclass
class OutcomeContract:
    goal: str
    summary: str
    conditions: list[Condition]
    generated_by: str = "deterministic-fallback"


@dataclass
class Evidence:
    condition_id: str
    condition_name: str
    verdict: str
    method: str
    expected: str
    observed: str
    details: dict[str, Any]
    timestamp: str


@dataclass
class VerificationReport:
    verdict: str
    passed: int
    failed: int
    blocked: int
    total: int
    evidence: list[Evidence]


def default_contract(goal: str) -> OutcomeContract:
    """A deterministic contract for the built-in deployment/auth demo."""
    return OutcomeContract(
        goal=goal,
        summary="Production deployment is reachable and authentication works end to end without regressing the smoke suite.",
        generated_by="deterministic-fallback",
        conditions=[
            Condition(
                id="c1",
                name="Production reachable",
                description="The deployed application responds successfully.",
                critical=True,
                method="http_status",
                params={"path": "/", "expected_status": 200},
            ),
            Condition(
                id="c2",
                name="Login UI renders",
                description="The login page is available and contains the login form.",
                critical=True,
                method="body_contains",
                params={"path": "/login", "contains": "Sign in"},
            ),
            Condition(
                id="c3",
                name="Valid authentication succeeds",
                description="Known-valid credentials create an authenticated session.",
                critical=True,
                method="valid_login",
                params={"path": "/api/login", "username": "demo@proof.local", "password": "correct-password"},
            ),
            Condition(
                id="c4",
                name="Invalid authentication is rejected",
                description="Incorrect credentials are rejected.",
                critical=True,
                method="invalid_login",
                params={"path": "/api/login", "username": "demo@proof.local", "password": "wrong-password"},
            ),
            Condition(
                id="c5",
                name="Authenticated dashboard loads",
                description="A valid authenticated session can reach the dashboard.",
                critical=True,
                method="dashboard_access",
                params={"path": "/dashboard", "contains": "Deployment dashboard"},
            ),
            Condition(
                id="c6",
                name="Regression smoke suite passes",
                description="Critical smoke tests remain healthy after the change.",
                critical=True,
                method="regression_api",
                params={"path": "/health/regression"},
            ),
        ],
    )


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def generate_contract_with_nebius(goal: str) -> OutcomeContract:
    """Generate an outcome contract with NVIDIA Nemotron through Nebius Token Factory.

    Falls back cleanly when no API key is configured or the provider is unavailable.
    The available methods are intentionally constrained to deterministic verifiers.
    """
    api_key = os.getenv("NEBIUS_API_KEY")
    if not api_key:
        return default_contract(goal)

    model = os.getenv("NEBIUS_MODEL", "nvidia/Nemotron-3_5-Lightning")
    base_url = os.getenv("NEBIUS_BASE_URL", "https://api.tokenfactory.nebius.com/v1")
    system = """You are PROOF's Outcome Contract Engineer. Convert a user's software/deployment goal into a small set of independently verifiable success conditions. Do not claim success. Return strict JSON only. You may ONLY use these verifier methods: http_status, body_contains, valid_login, invalid_login, dashboard_access, regression_api. Keep the demo endpoints exactly as shown in the example schema because the verifier is connected to a controlled test application. Prefer deterministic evidence over model judgment."""
    user = f"""Goal: {goal}

Return this exact JSON shape:
{{
  "goal": "...",
  "summary": "...",
  "conditions": [
    {{"id":"c1","name":"Production reachable","description":"...","critical":true,"method":"http_status","params":{{"path":"/","expected_status":200}}}},
    {{"id":"c2","name":"Login UI renders","description":"...","critical":true,"method":"body_contains","params":{{"path":"/login","contains":"Sign in"}}}},
    {{"id":"c3","name":"Valid authentication succeeds","description":"...","critical":true,"method":"valid_login","params":{{"path":"/api/login","username":"demo@proof.local","password":"correct-password"}}}},
    {{"id":"c4","name":"Invalid authentication is rejected","description":"...","critical":true,"method":"invalid_login","params":{{"path":"/api/login","username":"demo@proof.local","password":"wrong-password"}}}},
    {{"id":"c5","name":"Authenticated dashboard loads","description":"...","critical":true,"method":"dashboard_access","params":{{"path":"/dashboard","contains":"Deployment dashboard"}}}},
    {{"id":"c6","name":"Regression smoke suite passes","description":"...","critical":true,"method":"regression_api","params":{{"path":"/health/regression"}}}}
  ]
}}"""

    try:
        endpoint = base_url.rstrip("/") + "/chat/completions"
        response = httpx.post(
            endpoint,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.1,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        raw = payload["choices"][0]["message"]["content"] or ""
        data = _extract_json(raw)
        allowed = {"http_status", "body_contains", "valid_login", "invalid_login", "dashboard_access", "regression_api"}
        conditions: list[Condition] = []
        for item in data.get("conditions", []):
            method = item.get("method")
            if method not in allowed:
                continue
            conditions.append(
                Condition(
                    id=str(item.get("id", f"c{len(conditions)+1}")),
                    name=str(item.get("name", "Verification condition")),
                    description=str(item.get("description", "")),
                    critical=bool(item.get("critical", True)),
                    method=method,
                    params=dict(item.get("params") or {}),
                )
            )
        # A contract with missing core checks is not accepted; deterministic fallback is safer.
        if len(conditions) < 5:
            return default_contract(goal)
        return OutcomeContract(
            goal=str(data.get("goal") or goal),
            summary=str(data.get("summary") or "Independent verification contract"),
            conditions=conditions,
            generated_by=f"nebius:{model}",
        )
    except Exception:
        return default_contract(goal)


async def verify_contract(base_url: str, contract: OutcomeContract) -> VerificationReport:
    evidence: list[Evidence] = []
    context: dict[str, Any] = {}
    timeout = httpx.Timeout(8.0, connect=3.0)

    async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, follow_redirects=True) as client:
        for condition in contract.conditions:
            timestamp = datetime.now(timezone.utc).isoformat()
            verdict = "BLOCKED"
            expected = ""
            observed = ""
            details: dict[str, Any] = {}
            try:
                p = condition.params
                if condition.method == "http_status":
                    expected_status = int(p.get("expected_status", 200))
                    response = await client.get(p.get("path", "/"))
                    verdict = "PASS" if response.status_code == expected_status else "FAIL"
                    expected = f"HTTP {expected_status}"
                    observed = f"HTTP {response.status_code}"
                    details = {"url": str(response.url), "body_preview": response.text[:300]}

                elif condition.method == "body_contains":
                    response = await client.get(p.get("path", "/"))
                    needle = str(p.get("contains", ""))
                    ok = response.status_code == 200 and needle.lower() in response.text.lower()
                    verdict = "PASS" if ok else "FAIL"
                    expected = f"HTTP 200 and body contains {needle!r}"
                    observed = f"HTTP {response.status_code}; contains={needle.lower() in response.text.lower()}"
                    details = {"url": str(response.url), "body_preview": response.text[:300]}

                elif condition.method == "valid_login":
                    response = await client.post(
                        p.get("path", "/api/login"),
                        json={"username": p.get("username"), "password": p.get("password")},
                    )
                    payload: dict[str, Any] = {}
                    try:
                        payload = response.json()
                    except Exception:
                        pass
                    token = payload.get("token")
                    if response.status_code == 200 and token:
                        context["auth_token"] = token
                        verdict = "PASS"
                    else:
                        verdict = "FAIL"
                    expected = "HTTP 200 with session token"
                    observed = f"HTTP {response.status_code}; token={'present' if token else 'missing'}"
                    details = {"url": str(response.url), "response": payload or response.text[:300]}

                elif condition.method == "invalid_login":
                    response = await client.post(
                        p.get("path", "/api/login"),
                        json={"username": p.get("username"), "password": p.get("password")},
                    )
                    verdict = "PASS" if response.status_code in (400, 401, 403) else "FAIL"
                    expected = "Invalid credentials rejected (HTTP 400/401/403)"
                    observed = f"HTTP {response.status_code}"
                    try:
                        details = {"url": str(response.url), "response": response.json()}
                    except Exception:
                        details = {"url": str(response.url), "response": response.text[:300]}

                elif condition.method == "dashboard_access":
                    token = context.get("auth_token")
                    if not token:
                        verdict = "BLOCKED"
                        expected = "Authenticated dashboard available"
                        observed = "No verified authentication token was available from the previous condition"
                        details = {"dependency": "valid_login"}
                    else:
                        response = await client.get(
                            p.get("path", "/dashboard"),
                            headers={"Authorization": f"Bearer {token}"},
                        )
                        needle = str(p.get("contains", ""))
                        ok = response.status_code == 200 and needle.lower() in response.text.lower()
                        verdict = "PASS" if ok else "FAIL"
                        expected = f"HTTP 200 authenticated response containing {needle!r}"
                        observed = f"HTTP {response.status_code}; contains={needle.lower() in response.text.lower()}"
                        details = {"url": str(response.url), "body_preview": response.text[:300]}

                elif condition.method == "regression_api":
                    response = await client.get(p.get("path", "/health/regression"))
                    payload: dict[str, Any] = {}
                    try:
                        payload = response.json()
                    except Exception:
                        pass
                    ok = response.status_code == 200 and payload.get("passed") is True
                    verdict = "PASS" if ok else "FAIL"
                    expected = "Regression endpoint reports passed=true"
                    observed = f"HTTP {response.status_code}; passed={payload.get('passed')}"
                    details = {"url": str(response.url), "response": payload or response.text[:300]}
                else:
                    verdict = "BLOCKED"
                    expected = "Supported verifier method"
                    observed = f"Unsupported method {condition.method}"

            except Exception as exc:
                verdict = "BLOCKED"
                expected = "Verifier completes and returns evidence"
                observed = f"Verifier error: {type(exc).__name__}: {exc}"
                details = {"error_type": type(exc).__name__}

            evidence.append(
                Evidence(
                    condition_id=condition.id,
                    condition_name=condition.name,
                    verdict=verdict,
                    method=condition.method,
                    expected=expected,
                    observed=observed,
                    details=details,
                    timestamp=timestamp,
                )
            )

    passed = sum(1 for e in evidence if e.verdict == "PASS")
    failed = sum(1 for e in evidence if e.verdict == "FAIL")
    blocked = sum(1 for e in evidence if e.verdict == "BLOCKED")
    critical_by_id = {c.id: c.critical for c in contract.conditions}
    critical_problem = any(e.verdict != "PASS" and critical_by_id.get(e.condition_id, True) for e in evidence)
    verdict = "VERIFIED" if not critical_problem else ("FAILED" if failed else "BLOCKED")
    return VerificationReport(
        verdict=verdict,
        passed=passed,
        failed=failed,
        blocked=blocked,
        total=len(evidence),
        evidence=evidence,
    )


def contract_to_dict(contract: OutcomeContract) -> dict[str, Any]:
    return {
        "goal": contract.goal,
        "summary": contract.summary,
        "generated_by": contract.generated_by,
        "conditions": [asdict(c) for c in contract.conditions],
    }


def report_to_dict(report: VerificationReport) -> dict[str, Any]:
    return {
        "verdict": report.verdict,
        "passed": report.passed,
        "failed": report.failed,
        "blocked": report.blocked,
        "total": report.total,
        "evidence": [asdict(e) for e in report.evidence],
    }
