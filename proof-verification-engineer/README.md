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
- Optional NVIDIA Nemotron contract generation through Nebius Token Factory.
- Deterministic HTTP/API verification tools.
- A controlled coding/deployment demo with a realistic production-only auth failure.
- Evidence-backed FAIL / BLOCKED / VERIFIED verdicts.
- Repair → re-verification loop using the **same frozen contract**.
- SQLite Proof Pack persistence.
- Dashboard and audit trail UI.
- Zero-cost local fallback when no model key is present.

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

No Nebius key is needed for the local demo; the app uses a deterministic contract fallback so development never depends on paid APIs.

## Enable NVIDIA Nemotron on Nebius Token Factory

Create `.env` or set environment variables:

```bash
NEBIUS_API_KEY=your_key
NEBIUS_BASE_URL=https://api.tokenfactory.nebius.com/v1
NEBIUS_MODEL=nvidia/Nemotron-3_5-Lightning
```

Then export/load the variables before starting the server.

PROOF uses Nemotron for the reasoning-heavy step: converting an ambiguous human goal into a constrained Outcome Contract. Deterministic software still evaluates HTTP status, authentication behavior, and regression state.

> Design principle: **Do not ask an LLM what reality can answer directly.**

## API

- `GET /api/health`
- `POST /api/contracts/generate`
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
    └──── future: browser / shell verifier
    │
    ▼
Evidence Store
    │
    ├──── VERIFIED ───► Proof Pack
    │
    └──── FAILED ─────► Repair context ─► Worker ─► Reverify
```

## Submission roadmap

Next additions should be made in this order:

1. Playwright browser verifier + screenshots.
2. Shell/test-runner verifier inside an isolated local sandbox.
3. Adapter interface for an external worker agent.
4. Verification-plan generation for arbitrary web-app tasks.
5. Controlled reliability benchmark (true success vs false completion).
6. Small second-domain proof to demonstrate generality.
7. Hosted demo and 3-minute submission video.

Do **not** expand into observability, generic benchmarks, security scanning, or a full agent framework before the verification loop is reliable.

## Cost policy

The project is designed to run locally without paid infrastructure. Nebius calls are optional in development and should use hackathon/free credits. No paid database, vector store, monitoring service, browser cloud, or proprietary worker-agent API is required.

## Limitations

- The current execution tools are deliberately constrained to the included web-app verification scenario.
- A `VERIFIED` result means the defined Outcome Contract passed against collected evidence. It is not a mathematical proof of universal correctness.
- Browser screenshots and isolated shell verification are roadmap items, not silently simulated features.

## License

MIT
