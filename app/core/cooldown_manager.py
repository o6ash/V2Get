"""Cooldown bookkeeping keyed by fingerprint.

A config that fails TCP validation ``fail_threshold`` times in a row is evicted
from the active pool and placed in cooldown for ``cooldown_hours``. While in
cooldown its fingerprint is ignored entirely (never re-added). A successful
validation resets the fail counter and clears any cooldown.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_manager import settings
from app.models import Cooldown, utcnow


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def is_in_cooldown(session: AsyncSession, fingerprint: str) -> bool:
    row = await session.get(Cooldown, fingerprint)
    if not row or row.cooldown_until is None:
        return False
    until = _aware(row.cooldown_until)
    if until and until > datetime.now(timezone.utc):
        return True
    # Expired — clear the cooldown window but keep the row for history.
    row.cooldown_until = None
    return False


async def record_success(session: AsyncSession, fingerprint: str) -> None:
    row = await session.get(Cooldown, fingerprint)
    if row:
        row.fail_count = 0
        row.last_seen = utcnow()
        row.cooldown_until = None


async def record_failure(session: AsyncSession, fingerprint: str) -> bool:
    """Increment the failure counter. Returns True if this trips cooldown."""
    row = await session.get(Cooldown, fingerprint)
    if not row:
        row = Cooldown(fingerprint=fingerprint, fail_count=0)
        session.add(row)
    row.fail_count += 1
    row.last_seen = utcnow()
    if row.fail_count >= settings.fail_threshold:
        row.cooldown_until = datetime.now(timezone.utc) + timedelta(
            hours=settings.cooldown_hours
        )
        return True
    return False


async def list_cooldowns(session: AsyncSession, active_only: bool = True) -> list[dict]:
    rows = (await session.execute(select(Cooldown))).scalars().all()
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for r in rows:
        until = _aware(r.cooldown_until)
        remaining = int((until - now).total_seconds()) if until and until > now else 0
        if active_only and remaining <= 0:
            continue
        out.append(
            {
                "fingerprint": r.fingerprint,
                "fail_count": r.fail_count,
                "last_seen": _aware(r.last_seen).isoformat() if r.last_seen else None,
                "cooldown_until": until.isoformat() if until else None,
                "remaining_seconds": remaining,
            }
        )
    out.sort(key=lambda x: x["remaining_seconds"], reverse=True)
    return out


async def active_cooldown_count(session: AsyncSession) -> int:
    rows = (await session.execute(select(Cooldown))).scalars().all()
    now = datetime.now(timezone.utc)
    return sum(1 for r in rows if (u := _aware(r.cooldown_until)) and u > now)


async def remove_cooldown(session: AsyncSession, fingerprint: str) -> None:
    await session.execute(delete(Cooldown).where(Cooldown.fingerprint == fingerprint))


async def reset_fail_count(session: AsyncSession, fingerprint: str) -> None:
    row = await session.get(Cooldown, fingerprint)
    if row:
        row.fail_count = 0
        row.cooldown_until = None
