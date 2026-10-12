"""Offline-only, background v0.7 run coordinator and redacted Proof Pack reader.

This is a presentation boundary. It never calls Token Factory and never decides a
verification verdict. The persisted v0.7 Proof Packs remain authoritative.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Callable
from uuid import uuid4

from app.core.agent_verification_loop import AgentVerificationLoop, LoopOutcome
from app.core.intelligence import PlanningError
from app.demo_v05 import GOAL
from app.demo_v07 import OfflineFullLoopModel
from app.workers.nemotron_coding import WorkerDecisionError


MAX_JOBS = 12
MAX_PACK_BYTES = 2_000_000
DEFAULT_RUN_TIMEOUT_SECONDS = 300.0
_ID = re.compile(r"^[0-9a-f]{32}$")
_RUN_ID = re.compile(r"^[0-9a-f]{32}_[12]$")
_SAFE_CODE = re.compile(r"^[a-z][a-z_]{0,47}$")
_SAFE_MODEL = re.compile(r"^[A-Za-z0-9_./-]{1,96}$")
_SENSITIVE_TEXT = re.compile(
    r"(?i)(nvapi-[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]+|Bearer\s+\S+|"
    r"fake-test-password|[A-Za-z]:\\[^\s]+|https?://[^\s]+)"
)
_STAGES = frozenset({
    "worker_running", "worker_completed", "contract_frozen", "plan_validated",
    "verification_running", "first_pack_saved", "failed_evidence_confirmed",
    "repair_running", "repair_applied", "reverification_running", "second_pack_saved",
})


class RunConflict(Exception):
    pass


class RunNotFound(Exception):
    pass


class PackUnavailable(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _label(value: object, limit: int = 300) -> str:
    if not isinstance(value, str):
        return "[unavailable]"
    return _SENSITIVE_TEXT.sub("[redacted]", value[:limit]).replace("\r", " ").replace("\n", " ")


def _symbol(value: object) -> str | None:
    return value if isinstance(value, str) and _SAFE_MODEL.fullmatch(value) else None


def _observation(value: object, verifier: str, operation: str) -> object:
    """Only known scalar observations cross the browser boundary."""
    if value is None or type(value) in (bool, int):
        return value
    if verifier == "browser" and operation in ("fill", "click", "wait_for"):
        return value if value in ("filled", "clicked", "ready") else "[redacted]"
    if verifier == "browser" and operation == "assert_text":
        return value if value == "Deployment dashboard" else "[redacted text]"
    if verifier == "engine" and value in ("VERIFIED", "FAILED", "BLOCKED", "INCONCLUSIVE"):
        return value
    return "[redacted]"


def _safe_evidence(item: dict) -> dict:
    verifier = item.get("verifier_type")
    operation = item.get("operation")
    status = item.get("status")
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    safe_metadata = {}
    if verifier == "http":
        if type(metadata.get("status_code")) is int:
            safe_metadata["status_code"] = metadata["status_code"]
        if metadata.get("method") == "GET":
            safe_metadata["method"] = "GET"
    error_category = None
    if item.get("error") is not None:
        error_category = ("prerequisite_not_verified" if verifier == "engine" else
                          "verification_failed" if status == "FAILED" else "verification_unavailable")
    return {
        "evidence_id": _symbol(item.get("evidence_id")),
        "condition_id": _symbol(item.get("condition_id")),
        "verifier": verifier if verifier in ("http", "browser", "shell", "engine") else "unknown",
        "operation": _symbol(operation),
        "status": status if status in ("VERIFIED", "FAILED", "BLOCKED", "INCONCLUSIVE") else "INCONCLUSIVE",
        "expected": _observation(item.get("expected"), verifier, operation),
        "observed": _observation(item.get("observed"), verifier, operation),
        "error_category": error_category,
        "metadata": safe_metadata,
        "timestamp": item.get("timestamp") if isinstance(item.get("timestamp"), str) else None,
    }


def _safe_pack(raw: dict, source_hash: str) -> dict:
    intelligence = raw.get("intelligence") if isinstance(raw.get("intelligence"), dict) else {}
    contract = intelligence.get("contract") if isinstance(intelligence.get("contract"), dict) else {}
    plan = intelligence.get("plan") if isinstance(intelligence.get("plan"), dict) else {}
    worker_decisions = intelligence.get("worker_model_decisions") or []
    claim = intelligence.get("worker_claim") or {}
    usage = intelligence.get("model_usage") or {}
    repair_request = intelligence.get("repair_request") or {}
    repair_actions = intelligence.get("repair_actions") or []
    conditions = []
    for item in contract.get("conditions", []):
        conditions.append({"id": _symbol(item.get("id")),
                           "acceptance_ref": _symbol(item.get("acceptance_ref")),
                           "description": _label(item.get("description")),
                           "critical": item.get("critical") is True,
                           "prerequisites": [_symbol(dep) for dep in item.get("prerequisites", [])]})
    plan_conditions = []
    for item in plan.get("conditions", []):
        plan_conditions.append({"condition_id": _symbol(item.get("condition_id")),
                                "steps": [{"verifier": _symbol(step.get("verifier")),
                                           "operation": _symbol(step.get("operation"))}
                                          for step in item.get("steps", [])]})
    condition_results = [{"condition_id": _symbol(item.get("condition_id")),
                          "description": _label(item.get("description")),
                          "critical": item.get("critical") is True,
                          "status": item.get("status"),
                          "evidence_ids": [_symbol(eid) for eid in item.get("evidence_ids", [])],
                          "blocked_by": _symbol(item.get("blocked_by"))}
                         for item in raw.get("condition_results", [])]
    first_decision = worker_decisions[0] if worker_decisions else {}
    repair_decision = worker_decisions[-1] if len(worker_decisions) > 1 else {}
    repair_evidence = repair_request.get("failed_evidence") or []
    return {
        "format": "proof_pack_v3_redacted",
        "redacted": True,
        "source_sha256": source_hash,
        "run_id": raw.get("run_id"),
        "contract_id": raw.get("contract_id"),
        "contract_version": raw.get("contract_version"),
        "goal": GOAL,
        "verdict": raw.get("verdict"),
        "started_at": raw.get("started_at"),
        "completed_at": raw.get("completed_at"),
        "duration_seconds": raw.get("duration_seconds"),
        "execution_mode": intelligence.get("model_execution"),
        "plan_fingerprint": intelligence.get("plan_fingerprint"),
        "worker": {"claim": claim.get("status"), "model_id": _symbol(first_decision.get("model_id")),
                   "code_action": first_decision.get("action") if first_decision.get("action") == "propose_code_patch" else None},
        "contract": {"conditions": conditions,
                     "compiler": {"model_id": _symbol(contract.get("provenance", {}).get("model_id")),
                                  "prompt_version": _symbol(contract.get("provenance", {}).get("prompt_version"))}},
        "plan": {"conditions": plan_conditions,
                 "planner": {"model_id": _symbol(intelligence.get("plan_provenance", {}).get("model_id")),
                             "prompt_version": _symbol(intelligence.get("plan_provenance", {}).get("prompt_version"))}},
        "condition_results": condition_results,
        "evidence": [_safe_evidence(item) for item in raw.get("evidence", [])],
        "failed_evidence_ids": [_symbol(eid) for eid in intelligence.get("failed_evidence_ids", [])],
        "repair": ({"migration_ref": "auth_sessions_v1" if repair_decision.get("selected_value") == "auth_sessions_v1" else None,
                    "action": "apply_authorized_migration" if any(item.get("action") == "apply_authorized_migration" for item in repair_actions) else None,
                    "outcome": "applied" if any(item.get("outcome") == "applied" for item in repair_actions) else None,
                    "evidence_ids": [_symbol(item.get("evidence_id")) for item in repair_evidence]}
                   if repair_request else None),
        "usage": {key: usage.get(key) if type(usage.get(key)) is int else None for key in (
            "attempted_requests", "completed_requests", "provider_requests_attempted",
            "provider_request_count", "prompt_tokens", "completion_tokens", "reasoning_tokens")},
        "redacted_fields": ["HTTP response bodies", "subprocess output", "filesystem paths",
                            "browser artifacts", "credential references", "raw verifier errors"],
    }


@dataclass
class _Job:
    job_id: str
    root: Path
    created_at: str
    status: str = "queued"
    stage: str = "queued"
    updated_at: str = ""
    events: list[dict] = field(default_factory=list)
    pack_hashes: dict[str, str] = field(default_factory=dict)
    contract_id: str | None = None
    plan_fingerprint: str | None = None
    worker_claim: str | None = None
    worker_model: str | None = None
    error_code: str | None = None
    started_monotonic: float | None = None
    active: bool = True


Runner = Callable[[Path, Callable[[str, dict], None]], LoopOutcome]


def _offline_runner(root: Path, progress: Callable[[str, dict], None]) -> LoopOutcome:
    return AgentVerificationLoop(OfflineFullLoopModel(), "offline_mock", root).run(on_progress=progress)


class WorkspaceRunService:
    def __init__(self, runner: Runner | None = None, *, root: Path | None = None,
                 timeout_seconds: float = DEFAULT_RUN_TIMEOUT_SECONDS) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Run timeout must be positive")
        self.timeout_seconds = timeout_seconds
        self.root = (root or Path(tempfile.mkdtemp(prefix="proof-v08-packs-"))).resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        if self.root == temp_root or not self.root.is_relative_to(temp_root):
            raise ValueError("Workspace Proof Packs must stay under the OS temporary directory")
        self.root.mkdir(parents=True, exist_ok=True)
        self._runner = runner or _offline_runner
        self._lock = RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="proof-workspace")
        self._jobs: dict[str, _Job] = {}
        self._latest: str | None = None

    def start(self) -> dict:
        with self._lock:
            if any(job.active for job in self._jobs.values()):
                raise RunConflict("A verification run is already active")
            if len(self._jobs) >= MAX_JOBS:
                raise RunConflict("This workspace session has reached its run limit")
            job_id = uuid4().hex
            job = _Job(job_id, self.root / job_id, _now())
            job.root.mkdir(parents=True, exist_ok=False)
            job.updated_at = job.created_at
            self._jobs[job_id] = job
            self._latest = job_id
            self._pool.submit(self._execute, job_id)
            return self._snapshot(job)

    def _progress(self, job_id: str, stage: str, data: dict) -> None:
        if stage not in _STAGES:
            raise ValueError("Unknown internal run stage")
        with self._lock:
            job = self._jobs[job_id]
            event = {"stage": stage, "at": _now()}
            if stage == "worker_completed":
                job.worker_claim = data["claim"] if data.get("claim") in ("COMPLETED", "FAILED") else None
                job.worker_model = _symbol(data.get("model_id"))
            elif stage == "contract_frozen":
                job.contract_id = _symbol(data.get("contract_id"))
            elif stage == "plan_validated":
                job.plan_fingerprint = _symbol(data.get("plan_fingerprint"))
            elif stage in ("first_pack_saved", "second_pack_saved"):
                run_id = data.get("run_id")
                if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
                    raise ValueError("Invalid internal Proof Pack ID")
                path = (job.root / f"{run_id}.json").resolve()
                if path.parent != job.root.resolve() or not path.is_file() or path.stat().st_size > MAX_PACK_BYTES:
                    raise ValueError("Persisted Proof Pack is unavailable or oversized")
                job.pack_hashes[run_id] = hashlib.sha256(path.read_bytes()).hexdigest()
                event["run_id"] = run_id
                event["verdict"] = data.get("verdict") if data.get("verdict") in ("FAILED", "VERIFIED", "BLOCKED", "INCONCLUSIVE") else None
            elif stage == "failed_evidence_confirmed":
                event["evidence_id"] = _symbol(data.get("evidence_id"))
            elif stage == "repair_applied":
                event["migration_ref"] = "auth_sessions_v1" if data.get("migration_ref") == "auth_sessions_v1" else None
            if job.status != "timeout":
                job.stage = stage
            job.updated_at = event["at"]
            job.events.append(event)

    def _execute(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.status = "running"
            job.started_monotonic = time.monotonic()
            job.updated_at = _now()
        try:
            result = self._runner(job.root, lambda stage, data: self._progress(job_id, stage, data))
            if (result.execution_mode != "offline_mock" or result.first.verdict != "FAILED"
                    or result.second.verdict != "VERIFIED"):
                raise ValueError("Offline run did not satisfy the controlled scenario")
            with self._lock:
                self._check_timeout(job)
                if (len(job.pack_hashes) != 2 or result.first.run_id not in job.pack_hashes
                        or result.second.run_id not in job.pack_hashes
                        or result.first.contract_id != result.second.contract_id
                        or job.contract_id != result.first.contract_id
                        or job.plan_fingerprint != result.plan_fingerprint):
                    raise ValueError("Persisted run identity is incomplete")
                if job.status != "timeout":
                    job.status = "complete"
                    job.stage = "complete"
                    job.updated_at = _now()
        except BaseException as exc:
            code = ("interrupted" if isinstance(exc, (KeyboardInterrupt, SystemExit, InterruptedError)) else
                    "timeout" if isinstance(exc, TimeoutError) else
                    exc.code if isinstance(exc, (PlanningError, WorkerDecisionError))
                    and _SAFE_CODE.fullmatch(exc.code) else "run_failed")
            with self._lock:
                if job.status != "timeout":
                    job.status = "timeout" if code == "timeout" else "error"
                    job.stage = job.status
                    job.error_code = code
                    job.updated_at = _now()
        finally:
            with self._lock:
                job.active = False

    def _check_timeout(self, job: _Job) -> None:
        if (job.active and job.status in ("queued", "running")
                and job.started_monotonic is not None
                and time.monotonic() - job.started_monotonic >= self.timeout_seconds):
            job.status = "timeout"
            job.stage = "timeout"
            job.error_code = "timeout"
            job.updated_at = _now()

    def _snapshot(self, job: _Job) -> dict:
        self._check_timeout(job)
        return {"job_id": job.job_id, "status": job.status, "stage": job.stage,
                "execution_mode": "offline_mock", "goal": GOAL,
                "created_at": job.created_at, "updated_at": job.updated_at,
                "events": [dict(item) for item in job.events],
                "pack_ids": list(job.pack_hashes),
                "contract_id": job.contract_id, "plan_fingerprint": job.plan_fingerprint,
                "worker_claim": job.worker_claim, "worker_model": job.worker_model,
                "error_code": job.error_code, "worker_active": job.active}

    def get(self, job_id: str) -> dict:
        if not _ID.fullmatch(job_id):
            raise RunNotFound
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise RunNotFound
            return self._snapshot(job)

    def latest(self) -> dict | None:
        with self._lock:
            return self._snapshot(self._jobs[self._latest]) if self._latest else None

    def list_recent(self) -> list[dict]:
        with self._lock:
            return [self._snapshot(job) for job in reversed(list(self._jobs.values()))]

    def get_pack(self, job_id: str, run_id: str) -> dict:
        if not _ID.fullmatch(job_id) or not _RUN_ID.fullmatch(run_id):
            raise RunNotFound
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or run_id not in job.pack_hashes:
                raise RunNotFound
            root, expected_hash = job.root, job.pack_hashes[run_id]
        path = (root / f"{run_id}.json").resolve()
        if path.parent != root.resolve() or not path.is_file() or path.stat().st_size > MAX_PACK_BYTES:
            raise PackUnavailable
        content = path.read_bytes()
        actual_hash = hashlib.sha256(content).hexdigest()
        if actual_hash != expected_hash:
            raise PackUnavailable
        try:
            raw = json.loads(content)
            if not isinstance(raw, dict) or raw.get("run_id") != run_id:
                raise ValueError
            return _safe_pack(raw, actual_hash)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise PackUnavailable from None

    def close(self) -> None:
        self._pool.shutdown(wait=True)
