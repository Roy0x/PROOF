import asyncio

from app.core.orchestrator import run_demo
from app.core.planner import default_contract
from app.evidence.collector import build_report
from app.evidence.models import Evidence


def test_report_is_verified_only_when_every_critical_condition_passes():
    contract = default_contract("Fix login and deploy")
    evidence = [Evidence(c.id, c.name, "PASS", c.method, "expected", "observed", {}, "now") for c in contract.conditions]
    assert build_report(contract, evidence).verdict == "VERIFIED"
    evidence[0].verdict = "FAIL"
    assert build_report(contract, evidence).verdict == "FAILED"


def test_orchestrator_reuses_the_original_contract_after_repair(monkeypatch):
    contracts = []
    repaired = []

    async def verify(_base_url, contract):
        contracts.append(contract)
        state = "PASS" if repaired else "FAIL"
        evidence = [Evidence(c.id, c.name, state, c.method, "expected", "observed", {}, "now") for c in contract.conditions]
        return build_report(contract, evidence)

    monkeypatch.setattr("app.core.orchestrator.generate_contract_with_nebius", default_contract)
    result = asyncio.run(run_demo("Fix login", "http://example.test", verify, lambda: repaired.append(True)))
    assert result["first_verification"]["verdict"] == "FAILED"
    assert result["final_verification"]["verdict"] == "VERIFIED"
    assert contracts[0] is contracts[1]
