# PROOF — Verification Engineer for AI Agents

> **Agents act. PROOF verifies.**

PROOF is an independent outcome-verification layer for autonomous AI agents. A worker can claim a task is complete; PROOF freezes what success means, checks the actual environment using deterministic tools, returns evidence when reality disagrees, and replays the same contract after repair.

## Why this exists

Autonomous agents increasingly modify code, deploy software, update systems, browse websites, and operate business tools. Their action trace is not the same thing as successful completion. PROOF separates **doing the work** from **proving the requested outcome exists**.

The hackathon MVP focuses on coding/deployment agents because the result can be verified with hard evidence.

## Core loop

```text
User goal
   ↓
Worker agent
   ↓
claims completion
   ↓
Outcome Contract
   ↓
independent verification
   ↓
PASS ───────────────→ Proof Pack
   │
  FAIL
   ↓
evidence → worker repair
   ↓
replay same contract
   ↓
VERIFIED
```

## What is already implemented

- A polished local web application.
- Outcome Contract schema.
- NVIDIA Nemotron contract compilation and bounded plan generation through Nebius Token Factory (v0.4 backend).
- Deterministic HTTP/API verification tools.
- A controlled coding/deployment demo with a realistic production-only auth failure.
- Evidence-backed FAIL / BLOCKED / VERIFIED verdicts.
- Repair → re-verification loop using the **same frozen contract**.
- SQLite Proof Pack persistence.
- Dashboard and audit trail UI.
- Zero-cost deterministic v0.3 demo and offline mocked v0.4 tests.

## Built-in demo

The worker receives:

> `Fix the login bug and deploy the application.`

It makes a plausible completion claim after local success, but the controlled production target still has a missing authentication schema migration.

PROOF independently tests:

1. Production reachable
2. Login UI renders
3. Valid authentication succeeds
4. Invalid authentication is rejected
5. Authenticated dashboard loads
6. Regression smoke suite passes

The first run catches the production-only failure. The evidence is returned to the worker, the demo worker repairs the deployment, and PROOF executes the **same** contract again. Only then is the result marked `VERIFIED`.

## Run locally — ₹0

Requirements: Python 3.11+

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Open `http://localhost:8000`.

No Nebius key is needed for the local v0.3 demo. The v0.4 preparation endpoint returns a structured error if live inference is unavailable.

## Enable NVIDIA Nemotron on Nebius Token Factory

Create `.env` or set environment variables:

```bash
NEBIUS_API_KEY=your_key
NEBIUS_BASE_URL=https://api.tokenfactory.nebius.com/v1
PROOF_NEMOTRON_MODEL=nvidia/Nemotron-3_5-Lightning
```

Then export/load the variables before starting the server.

The v0.4 backend uses Nemotron to compile a frozen Outcome Contract, then makes one sequential planner call per frozen condition. Each planner response contains only a bounded `steps` array; PROOF validates the steps against the trusted capability manifest and assembles the plan with the original condition IDs, order, and prerequisites. Nemotron cannot add conditions or decide the verdict. The v0.3 verifiers and deterministic VerificationEngine still collect evidence and calculate the verdict.

Set `PROOF_PRODUCTION_BASE_URL` in the server environment before calling `POST /api/v0.4/prepare` with `{"goal":"Fix the login bug and deploy the application."}`. Optional trusted configuration is `PROOF_WORKSPACE`, `PROOF_ROUTES_JSON`, `PROOF_SELECTORS_JSON`, `PROOF_TEST_USERNAME`, and `PROOF_TEST_PASSWORD`. The last two become credential references in model prompts and are resolved only during browser execution. The response contains the frozen contract and validated plan; the Python `IntelligenceWorkflow.verify` method runs that prepared plan and attaches provenance to Proof Pack v3. The existing web demo remains a v0.3 compatibility flow with a deterministic fallback when no key is configured.

The normal test suite uses fake model responses and never calls Nebius. Two separate live smoke tests are available only when `PROOF_RUN_LIVE_NEBIUS_TESTS=1` and `NEBIUS_API_KEY` are set. Run `./.venv/Scripts/python.exe -m pytest -q tests/live/test_nebius_smoke.py -s` for contract compilation or `./.venv/Scripts/python.exe -m pytest -q tests/live/test_nebius_planner_smoke.py -s` for a locally frozen, one-condition step-only plan. Each test makes one inference request, reports model and latency, and never prints the key. Development caching is deferred to keep v0.4's trust boundary simple.

> Design principle: **Do not ask an LLM what reality can answer directly.**

## API

- `GET /api/health`
- `POST /api/contracts/generate`
- `POST /api/v0.4/prepare`
- `POST /api/demo/run`
- `POST /api/demo/reset`
- `POST /api/demo/repair`
- `GET /api/proof-packs`
- `GET /api/proof-packs/{id}`

FastAPI docs: `http://localhost:8000/docs`

## Architecture

```text
Worker claim
    │
    ▼
PROOF Contract Engine ─────► NVIDIA Nemotron / Nebius
    │
    ▼
Verification Planner
    │
    ├──── HTTP verifier
    ├──── API verifier
    └──── browser / shell verifiers
    │
    ▼
Evidence Store
    │
    ├──── VERIFIED ───► Proof Pack
    │
    └──── FAILED ─────► Repair context ─► Worker ─► Reverify
```

## v0.5 controlled coding-worker loop

Run the offline end-to-end demonstration with `.\.venv\Scripts\python.exe -m app.demo_v05` on Windows. It creates a disposable task workspace under the OS temporary directory and saves two separate Proof Packs to another temporary directory; the printed paths remain available after the app and workspace shut down. No Nebius key, external network, Docker, or real deployment is used.

The deterministic worker edits a real login-service source file, runs local SQLite regression tests, and starts that code on `127.0.0.1` with a separate production SQLite database. The local database has the required `auth_sessions` table; the production database initially does not. The worker claims `COMPLETED` after local checks, but PROOF's existing HTTP, Browser, and Shell verifiers find that production authentication fails. A failed schema diagnostic supplies `expected=True, observed=False` evidence; the controlled repair applies the missing migration to the disposable production database. PROOF reruns the same frozen contract and validated plan, collecting fresh evidence and preserving the first `FAILED` Proof Pack before the second `VERIFIED` pack.

The contract and step-only planner use fixed local mock responses for this repeatable demo. This is a real state-changing worker and independent verification loop, **not** an autonomous AI coding agent or a live Nemotron inference run. The older in-memory web demo remains available for compatibility.

## Submission roadmap

The v0.4 backend covers contract compilation, plan validation, deterministic execution, and provenance. The UI remains the v0.3 demo; connecting its controlled login scenario to the v0.4 preparation endpoint is future work.

Do **not** expand into observability, generic benchmarks, security scanning, or a full agent framework before the verification loop is reliable.

## Cost policy

The project is designed to run locally without paid infrastructure. Nebius calls are optional in development and should use hackathon/free credits. No paid database, vector store, monitoring service, browser cloud, or proprietary worker-agent API is required.

## Limitations

- The current execution tools are deliberately constrained to the included web-app verification scenario.
- A `VERIFIED` result means the defined Outcome Contract passed against collected evidence. It is not a mathematical proof of universal correctness.
- The live Nemotron smoke test remains opt-in and requires a configured API key.

## License

MIT
