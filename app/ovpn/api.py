"""REST API for the ovpn module, mounted under ``/api/ovpn``."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.ovpn.service import service

router = APIRouter(prefix="/ovpn", tags=["ovpn"])


class OvpnSettingsUpdate(BaseModel):
    values: dict[str, Any]


@router.get("/state")
async def ovpn_state() -> dict:
    return await service.state()


@router.get("/settings")
async def ovpn_get_settings() -> dict:
    return {"values": service.settings(), "defaults": service.defaults()}


@router.put("/settings")
async def ovpn_update_settings(payload: OvpnSettingsUpdate) -> dict:
    return {"values": await service.update_settings(payload.values)}


@router.post("/scan")
async def ovpn_scan() -> dict:
    service.trigger_scan()
    return {"status": "scan_triggered"}


@router.get("/files")
async def ovpn_files(limit: int = Query(100, le=1000)) -> list[dict]:
    return await service.recent_files(limit)
