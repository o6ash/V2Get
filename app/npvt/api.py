"""REST API for the npvt module, mounted under ``/api/npvt``.

Kept in its own router so the core :mod:`app.api.routes` is untouched; the app
includes it alongside the main router with the same auth dependency.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.npvt.service import service

router = APIRouter(prefix="/npvt", tags=["npvt"])


class NpvtSettingsUpdate(BaseModel):
    values: dict[str, Any]


@router.get("/state")
async def npvt_state() -> dict:
    return await service.state()


@router.get("/settings")
async def npvt_get_settings() -> dict:
    return {"values": service.settings(), "defaults": service.defaults()}


@router.put("/settings")
async def npvt_update_settings(payload: NpvtSettingsUpdate) -> dict:
    values = await service.update_settings(payload.values)
    return {"values": values}


@router.post("/scan")
async def npvt_scan() -> dict:
    service.trigger_scan()
    return {"status": "scan_triggered"}


@router.get("/files")
async def npvt_files(limit: int = Query(100, le=1000)) -> list[dict]:
    return await service.recent_files(limit)


@router.post("/queue/clear")
async def npvt_clear_queue() -> dict:
    deleted = await service.clear_queue()
    return {"status": "cleared", "deleted": deleted}


@router.post("/files/{file_id}/retry")
async def npvt_retry(file_id: int) -> dict:
    ok = await service.retry_file(file_id)
    if not ok:
        raise HTTPException(404, "file not found")
    return {"status": "requeued", "id": file_id}
