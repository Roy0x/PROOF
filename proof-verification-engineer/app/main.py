from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .engine import (
    contract_to_dict,
    generate_contract_with_nebius,
    report_to_dict,
    verify_contract,
)
from .store import get_pack, init_db, list_packs, save_pack

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(title="PROOF — Verification Engineer for AI Agents", version="0.1.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Controlled demo application state. The worker initially deploys a subtle production bug.
demo_state: dict[str, Any] = {"release": "broken", "repair_cycles": 0}


class DemoRunRequest(BaseModel):
    goal: str = "Fix the login bug and deploy the application."


class ContractRequest(BaseModel):
    goal: str


@app.on_event("startup")
def startup() -> None:
    init_db()


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "product": "PROOF",
        "nebius_configured": bool(os.getenv("NEBIUS_API_KEY")),
        "model": os.getenv("NEBIUS_MODEL", "nvidia/Nemotron-3_5-Lightning"),
    }


@app.post("/api/contracts/generate")
def generate_contract(request: ContractRequest) -> dict[str, Any]:
    contract = generate_contract_with_nebius(request.goal)
    return contract_to_dict(contract)


@app.post("/api/demo/reset")
def reset_demo() -> dict[str, Any]:
    demo_state["release"] = "broken"
    demo_state["repair_cycles"] = 0
    return {"status": "reset", "release": demo_state["release"]}


@app.post("/api/demo/repair")
def repair_demo() -> dict[str, Any]:
    demo_state["release"] = "fixed"
    demo_state["repair_cycles"] += 1
    return {
        "status": "repaired",
        "release": demo_state["release"],
        "repair_cycles": demo_state["repair_cycles"],
        "worker_action": "Applied missing production auth schema migration and redeployed.",
    }


@app.post("/api/demo/run")
async def run_demo(payload: DemoRunRequest, request: Request) -> dict[str, Any]:
    # 1) Worker claims completion while a production-only auth issue remains.
    demo_state["release"] = "broken"
    demo_state["repair_cycles"] = 0
    worker_claim = {
        "status": "COMPLETED",
        "message": "Login bug fixed locally, tests passed, and release deployed.",
        "release": "v1.0-demo",
    }

    # 2) PROOF freezes the success definition before verification.
    contract = generate_contract_with_nebius(payload.goal)
    contract_dict = contract_to_dict(contract)

    # 3) PROOF asks the environment instead of trusting the worker claim.
    base_url = str(request.base_url).rstrip("/") + "/demo-app"
    first_report = await verify_contract(base_url, contract)

    # 4) Structured evidence is returned to the worker; demo worker repairs the root cause.
    repair = None
    if first_report.verdict != "VERIFIED":
        failed = [e for e in first_report.evidence if e.verdict != "PASS"]
        repair = {
            "diagnosis": "Production authentication failed despite a successful deployment claim.",
            "evidence_forwarded": [
                {
                    "condition": e.condition_name,
                    "expected": e.expected,
                    "observed": e.observed,
                }
                for e in failed
            ],
            "worker_action": "Applied missing production auth schema migration and redeployed.",
        }
        demo_state["release"] = "fixed"
        demo_state["repair_cycles"] += 1

    # 5) Same frozen contract is replayed. The goalposts do not move.
    final_report = await verify_contract(base_url, contract)

    pack = {
        "goal": payload.goal,
        "worker_claim": worker_claim,
        "contract": contract_dict,
        "first_verification": report_to_dict(first_report),
        "repair": repair,
        "repair_cycles": demo_state["repair_cycles"],
        "final_verification": report_to_dict(final_report),
        "principle": "The worker does the task. PROOF independently verifies the resulting state.",
    }
    pack_id = save_pack(pack)
    pack["proof_id"] = pack_id
    return pack


@app.get("/api/proof-packs")
def proof_packs() -> list[dict[str, Any]]:
    return list_packs()


@app.get("/api/proof-packs/{pack_id}")
def proof_pack(pack_id: int) -> dict[str, Any]:
    pack = get_pack(pack_id)
    if not pack:
        raise HTTPException(status_code=404, detail="Proof Pack not found")
    pack["proof_id"] = pack_id
    return pack


# ------------------------------
# Controlled demo target app
# ------------------------------
@app.get("/demo-app/", response_class=HTMLResponse)
def demo_root() -> str:
    return """<!doctype html><html><body><h1>Acme Cloud</h1><a href='/demo-app/login'>Sign in</a></body></html>"""


@app.get("/demo-app/login", response_class=HTMLResponse)
def demo_login_page() -> str:
    return """<!doctype html><html><body><main><h1>Sign in</h1><form id='login-form'><input name='email'><input name='password' type='password'><button>Sign in</button></form></main></body></html>"""


class LoginPayload(BaseModel):
    username: str
    password: str


@app.post("/demo-app/api/login")
def demo_login(payload: LoginPayload) -> dict[str, Any]:
    if payload.username != "demo@proof.local" or payload.password != "correct-password":
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if demo_state["release"] == "broken":
        raise HTTPException(status_code=500, detail="DATABASE_SCHEMA_MISMATCH: auth_sessions relation missing")
    return {"token": "proof-demo-token", "user": payload.username}


@app.get("/demo-app/dashboard", response_class=HTMLResponse)
def demo_dashboard(request: Request) -> str:
    if request.headers.get("authorization") != "Bearer proof-demo-token":
        raise HTTPException(status_code=401, detail="Authentication required")
    return "<!doctype html><html><body><h1>Deployment dashboard</h1><p>Authenticated session verified.</p></body></html>"


@app.get("/demo-app/health/regression")
def regression_health() -> dict[str, Any]:
    return {"passed": True, "tests": 31, "failed": 0}
