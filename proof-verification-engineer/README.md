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
- Durable JSON Proof Packs for independent verification runs.
- v0.8 local verification workspace with a timeline, evidence explorer, repair view, and redacted Proof Pack exports.
- Zero-cost offline tests and a default web demo with mocked model decisions and real local verifier execution.

## Built-in demo

The worker receives:

> `Fix the login bug and deploy the application.`

It makes a plausible completion claim after local success, but the controlled production target still has a missing authentication schema migration.

In the default v0.8 workspace, PROOF independently tests:

1. The deployed home page responds with HTTP 200.
2. The production `auth_sessions` schema exists.
3. Valid production authentication succeeds.
4. Local regression tests pass.
5. The authenticated dashboard is accessible.

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
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/`.

No Nebius key is needed for the default v0.8 workspace. Its Run Verification button makes zero provider requests. Older HTTP endpoints that can call a live model are disabled by default; a developer must explicitly set `PROOF_ENABLE_LEGACY_LIVE_API=1` to use them. With that opt-in, the v0.4 preparation endpoint returns a structured error if live inference is unavailable.

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

The contract and step-only planner use fixed local mock responses for this repeatable demo. This is a real state-changing worker and independent verification loop, **not** an autonomous AI coding agent or a live Nemotron inference run. The older in-memory web demo remains available only with explicit local API opt-in.

## v0.6 bounded Nemotron coding worker

Run `.\.venv\Scripts\python.exe -m app.demo_v06` for the default, zero-cost offline mock. It makes two fake worker decisions while keeping the v0.5 app, production SQLite defect, real HTTP/Browser/Shell evidence, frozen contract, and separate FAILED/VERIFIED Proof Packs. The PROOF contract and step-only plan are still supplied by the offline fixture model, so this demo isolates the worker integration. Proof Packs are written outside the repository under the OS temporary directory; the task workspace and server are cleaned up.

The new `NemotronCodingWorkerAdapter` first writes the deliberately defective fixture and runs local tests. It sends the relevant trusted source plus a bounded, fake-credential-redacted failing test observation to its injectable model client. The model must choose a JSON action containing only a replacement expression for `password_matches` in `login_service.py`. Trusted code validates the expression with a narrow Python AST allowlist (one equality/inequality comparison of `candidate` and `stored`), verifies the exact source edit point, applies it, and reruns a fixed pytest command. A passing local run permits the worker to claim completion, but that claim cannot set PROOF's verdict.

After PROOF finds the real missing production `auth_sessions` table, the worker receives only the failed condition's evidence ID, operation, expected/observed values, and the single authorized migration reference. Nemotron chooses that reference through a second strict JSON action; trusted code checks the SQLite preconditions and executes fixed SQL. No model-provided SQL or shell command is run. PROOF then reuses the same frozen contract and plan with fresh evidence. Each Proof Pack records the worker action and model metadata (model ID, prompt version, token usage when available, latency) without storing prompts, reasoning text, passwords, or API keys. This is a bounded AI decision protocol, not unrestricted autonomous code execution or an OS sandbox.

For an explicit live worker demo only, set `PROOF_RUN_LIVE_AI_WORKER=1` and `NEBIUS_API_KEY`, then run `.\.venv\Scripts\python.exe -m app.demo_v06 --live`. That path uses the existing Nebius Token Factory client with `nvidia/Nemotron-3_5-Lightning` by default, `response_format=json_object`, disabled thinking, a 256-token cap per decision, and zero retries. It can make at most two worker inference requests; the default command and normal test suite never call Nebius. The first explicitly opted-in live demonstration completed the model-selected patch, local tests, evidence-driven migration, and independent FAILED-to-VERIFIED reverification. Further live runs remain opt-in. The v0.5 demo remains unchanged.

## v0.7 integrated Nemotron workflow

Run `.\.venv\Scripts\python.exe -m app.demo_v07` for the default offline demonstration. It uses one role-aware fake model for the coding worker, Outcome Contract compiler, and per-condition verification-step planner, while still applying a real source edit, running local tests, deploying the disposable login app on `127.0.0.1`, exercising real HTTP/Browser/Shell verifiers, migrating the separate production SQLite database, and saving separate FAILED and VERIFIED Proof Packs under the OS temporary directory. It makes no Nebius requests.

For a live full-loop smoke, `--live`, `PROOF_RUN_LIVE_FULL_LOOP=1`, and `NEBIUS_API_KEY` are all required. Unlike v0.6, one real Token Factory client serves **all three** model roles. Thinking is disabled and provider retries are zero. The central cap is eight inference requests: worker patch (1), contract compilation (1), one step-only plan per frozen condition (up to 5), and worker repair (1). More than five compiler conditions stop before planning. Live inference never runs in the normal test suite or default demo.

The first explicitly opted-in, fully live v0.7 demonstration succeeded with `nvidia/Nemotron-3_5-Lightning` via Nebius Token Factory: Nemotron selected the source patch, compiled the contract, planned all five checks, and selected the authorized `auth_sessions_v1` repair. Real local tests passed; independent PROOF verification reported FAILED before the production SQLite migration and VERIFIED afterward, with the frozen contract unchanged. All 8 of 8 inference requests completed; reported usage was 5,814 input tokens, 555 output tokens, and 0 reasoning tokens. These are observations from one controlled run, not a reliability or cost guarantee; any further live run remains explicitly opt-in and may incur provider usage.

A concise summary of the persisted live-run verdict, model roles, usage, and unchanged contract/plan is in [docs/live-nemotron-v07.md](docs/live-nemotron-v07.md). Raw live Proof Packs and credentials are not in the repository.

The scenario supplies five trusted acceptance references to the compiler: public home reachability, valid production authentication, authenticated dashboard, production session schema, and local regression tests. Nemotron proposes the condition IDs, descriptions, order, and dependencies; PROOF freezes and validates them. The planner sees the frozen condition and a trusted required-check hint, then proposes executable steps. PROOF rejects plans that do not cover the exact trusted operations and refs, including the independent `http.json_value` probe on `/health/schema` with `auth_sessions_exists=true`. These checks are scenario-specific; they do not turn model descriptions into evidence or let the model set a verdict.

After the first independent FAILED run, the v0.7 loop matches the schema failure to the current persisted Proof Pack, frozen plan, evidence ID, actual GET URL, and `expected=True/observed=False`. It uses the original model-generated condition ID, never an assumed `C2`. Only then may the worker model select the fixed `auth_sessions_v1` migration reference; trusted code checks database preconditions and applies fixed SQL. PROOF reverifies the **same** contract and plan with fresh evidence, preserving the first FAILED pack. Both packs include the immutable plan fingerprint, contract/plan provenance, worker decision metadata, actual request counts and usage when available, and `offline_mock` or `live_nebius` labeling. Raw prompts, reasoning, credentials, and API keys are not stored. This demonstration is limited to a disposable loopback login app, fake test credentials, a fixed migration allowlist, and exact scenario-specific acceptance checks. It is not a general autonomous deployment agent, a real production deployment, or an OS security sandbox.

## v0.8 local verification workspace

Start the local server with `.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000` from the project directory, then open `http://127.0.0.1:8000/`. Select **Run verification** once. The workspace always uses the **offline v0.7 scenario**: mocked, scripted model decisions, but a real isolated source edit, local tests, loopback app, independent HTTP/Browser/Shell checks, production SQLite migration, and two persisted Proof Packs. It never makes Nebius requests. The worker claim and PROOF verdict are deliberately separate; the timeline, evidence explorer, repair panel, and before/after comparison derive from actual backend events and packs. After completion, the hero shrinks to a results header and the outcome summary shows the four observed stages. Evidence IDs are shortened for readability; **Copy ID** copies the complete identifier, and Proof Pack inspection retains the complete redacted record. The failed-run explanation states the schema and dashboard observations without inferring a cause.

Runs execute in a background thread so polling remains responsive. A second run cannot overlap an active run. A run exceeding five minutes is marked timed out; its thread must finish before another run may begin. The browser reads only job-scoped, integrity-checked, redacted Proof Pack projections. **Inspect JSON** and **Download redacted JSON** do not expose raw pack files, HTTP bodies, subprocess output, screenshots, credential references, or local paths. Original packs remain in the OS temporary directory outside the repository; exports are filtered views and are not substitutes for the full internal artifacts. Run records live only for this server process; a restart clears the in-memory run index. There is a 12-run session limit and no history database or multi-user authentication. Bind the development server to loopback only. Legacy live-model API routes require the explicit server-side opt-in above; the v0.8 workspace never calls them.

## Windows judge test package

From this Git checkout, run `.\.venv\Scripts\python.exe scripts\build_judge_zip.py` to produce `dist/PROOF-v0.8-offline-windows.zip`. The ZIP uses repository-tracked application code and an explicit supporting-file inventory: fixtures, static UI, Windows launcher and setup helper, requirements, README, MIT license, and the redacted live-test summary. It excludes the virtual environment, untracked personal files, `.env`, Git metadata, Proof Packs, databases, screenshots, logs, and tests. The ZIP is for the **Windows offline demonstration**; no other operating system is claimed for this package.

Extract the archive fully, open the extracted folder, and double-click `run.bat`. The launcher creates or reuses a local virtual environment, checks installed dependencies without contacting a package index, installs packages only when missing, checks that Playwright Chromium starts, and asks before downloading Chromium when absent. First installation needs internet access and enough free disk space; allow at least 1 GB on the browser-cache drive. See `START_HERE_WINDOWS.txt` inside the ZIP for step-by-step instructions. The server binds to `127.0.0.1` only. Do not expose or forward port 8000: this controlled local workspace is not an authenticated public service.

The legacy `/api/demo/reset` and `/api/demo/repair` endpoints are disabled unless `PROOF_ENABLE_LEGACY_DEMO_API=1` is set locally. The old `/api/demo/run` also requires `PROOF_ENABLE_LEGACY_LIVE_API=1` because it can invoke Nemotron. The default `/api/workspace/runs` path never calls Nebius. These opt-ins are for local development, not authentication.

## Submission roadmap

The v0.8 workspace presents the existing controlled v0.7 loop without changing the deterministic verdict engine. General-purpose user-supplied goals and authenticated multi-user deployment remain future work.

Do **not** expand into observability, generic benchmarks, security scanning, or a full agent framework before the verification loop is reliable.

## Cost policy

The project is designed to run locally without paid infrastructure. Nebius calls are optional in development and should use hackathon/free credits. No paid database, vector store, monitoring service, browser cloud, or proprietary worker-agent API is required.

## Limitations

- The current execution tools are deliberately constrained to the included web-app verification scenario.
- A `VERIFIED` result means the defined Outcome Contract passed against collected evidence. It is not a mathematical proof of universal correctness.
- The live Nemotron smoke test remains opt-in and requires a configured API key.

## License

MIT
