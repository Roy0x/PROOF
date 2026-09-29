from __future__ import annotations

from .contracts import Condition, OutcomeContract


def default_contract(goal: str) -> OutcomeContract:
    """Deterministic contract for the built-in deployment/auth demo."""
    return OutcomeContract(
        goal=goal,
        summary="Production deployment is reachable and authentication works end to end without regressing the smoke suite.",
        conditions=[
            Condition("c1", "Production reachable", "The deployed application responds successfully.", True, "http_status", {"path": "/", "expected_status": 200}),
            Condition("c2", "Login UI renders", "The login page is available and contains the login form.", True, "body_contains", {"path": "/login", "contains": "Sign in"}),
            Condition("c3", "Valid authentication succeeds", "Known-valid credentials create an authenticated session.", True, "valid_login", {"path": "/api/login", "username": "demo@proof.local", "password": "correct-password"}),
            Condition("c4", "Invalid authentication is rejected", "Incorrect credentials are rejected.", True, "invalid_login", {"path": "/api/login", "username": "demo@proof.local", "password": "wrong-password"}),
            Condition("c5", "Authenticated dashboard loads", "A valid authenticated session can reach the dashboard.", True, "dashboard_access", {"path": "/dashboard", "contains": "Deployment dashboard"}),
            Condition("c6", "Regression smoke suite passes", "Critical smoke tests remain healthy after the change.", True, "regression_api", {"path": "/health/regression"}),
        ],
    )
