# PROOF — Independent Verification for AI Agents

**Agents act. PROOF verifies.** A worker's completion claim is tested against a frozen Outcome Contract. PROOF runs independent HTTP, browser, and shell checks, records evidence in Proof Packs, and repeats the same checks after an evidence-backed repair. The worker or model never sets PROOF's verdict.

## What is implemented

The v0.8 local workspace runs a controlled login-repair scenario. It shows the worker's **COMPLETED** claim beside PROOF's first **FAILED** verdict, the authorized `auth_sessions_v1` migration, and a fresh **VERIFIED** reverification. The contract and verification plan remain frozen across both runs. Both real Proof Packs can be inspected and exported through redacted, bounded endpoints.

NVIDIA `nvidia/Nemotron-3_5-Lightning` was integrated through the Nebius Token Factory API for the coding worker, Outcome Contract compiler, per-condition planner, and repair decision. The explicitly opted-in **live v0.7 test** completed eight model requests and reached FAILED → authorized repair → VERIFIED with real local verification. The default **v0.8 browser demo** uses an offline fixture for those model decisions while still performing the source edit, local tests, loopback deployment, independent verification, repair, and persisted Proof Packs. It makes **zero Nebius requests**. See [the live-test evidence summary](proof-verification-engineer/docs/live-nemotron-v07.md).

## Windows quick start

1. Install Python 3.11 or newer if it is not already available.
2. Download and extract the versioned Windows test ZIP, or open `proof-verification-engineer/` from this repository.
3. Double-click `run.bat` inside that folder. It creates or reuses a local `.venv`, installs missing Python packages, and checks Playwright Chromium. If Chromium is missing, it explains disk use and asks permission before downloading it.
4. Open <http://127.0.0.1:8000/> and select **Run verification**. Keep the terminal open.

The first installation needs internet access and adequate free disk space. The server binds to `127.0.0.1`; do not expose it on a public interface. Full judge instructions are in [START_HERE_WINDOWS.txt](proof-verification-engineer/START_HERE_WINDOWS.txt), and the detailed design and development history are in the [project README](proof-verification-engineer/README.md).

## Limits

This is a controlled disposable login-app scenario, not a general deployment agent or a sandbox for untrusted users. Runs are held in memory for one server process, concurrent runs are rejected, and the default workspace has no multi-user authentication. Live model use remains explicitly opt-in and may incur provider usage. Windows is the documented judge package target; other operating systems have not been validated for that package.

## License

MIT. See [LICENSE](LICENSE).
