"""Read-only evidence views and an explicitly offline v0.8 run action."""
from __future__ import annotations

import json

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import Response

from app.core.workspace_service import PackUnavailable, RunConflict, RunNotFound, WorkspaceRunService


router = APIRouter(prefix="/api/workspace", tags=["workspace"])
workspace_service = WorkspaceRunService()


@router.post("/runs", status_code=202)
def start_run(x_proof_workspace: str | None = Header(default=None)) -> dict:
    if x_proof_workspace != "offline":
        raise HTTPException(status_code=403, detail="Offline workspace header required")
    try:
        return workspace_service.start()
    except RunConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None


@router.get("/runs/current")
def current_run() -> dict | None:
    return workspace_service.latest()


@router.get("/runs")
def recent_runs() -> list[dict]:
    return workspace_service.list_recent()


@router.get("/runs/{job_id}")
def get_run(job_id: str) -> dict:
    try:
        return workspace_service.get(job_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Run not found") from None


@router.get("/runs/{job_id}/packs/{run_id}")
def get_pack(job_id: str, run_id: str) -> dict:
    try:
        return workspace_service.get_pack(job_id, run_id)
    except RunNotFound:
        raise HTTPException(status_code=404, detail="Proof Pack not found") from None
    except PackUnavailable:
        raise HTTPException(status_code=409, detail="Proof Pack integrity check failed") from None


@router.get("/runs/{job_id}/packs/{run_id}/export")
def export_pack(job_id: str, run_id: str) -> Response:
    pack = get_pack(job_id, run_id)
    return Response(json.dumps(pack, indent=2), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="proof-pack-{run_id}-redacted.json"',
                             "Cache-Control": "no-store",
                             "X-Content-Type-Options": "nosniff"})
