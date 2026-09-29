from app.engine import default_contract


def test_default_contract_has_critical_checks():
    contract = default_contract("Fix login and deploy")
    assert len(contract.conditions) == 6
    assert all(c.critical for c in contract.conditions)
    assert {c.method for c in contract.conditions} == {
        "http_status",
        "body_contains",
        "valid_login",
        "invalid_login",
        "dashboard_access",
        "regression_api",
    }


def test_goal_is_preserved():
    goal = "Deploy the app and make authentication work"
    assert default_contract(goal).goal == goal
