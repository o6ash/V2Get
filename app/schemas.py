"""Pydantic request/response models for the API."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChannelCreate(BaseModel):
    username: str = Field(..., min_length=1)
    scan_limit: int | None = Field(default=None, ge=1, le=1000)


class ChannelUpdate(BaseModel):
    enabled: bool | None = None
    last_message_id: int | None = None
    scan_limit: int | None = Field(default=None, ge=1, le=1000)


class BlacklistEntry(BaseModel):
    kind: str = Field(..., pattern="^(domains|ips|keywords)$")
    entry: str = Field(..., min_length=1)


class BlacklistReplace(BaseModel):
    kind: str = Field(..., pattern="^(domains|ips|keywords)$")
    entries: list[str] = Field(default_factory=list)


class SettingsUpdate(BaseModel):
    values: dict[str, Any]


class FingerprintRef(BaseModel):
    fingerprint: str = Field(..., min_length=1)
