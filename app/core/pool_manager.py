"""Rotating active-pool management.

Keeps the active pool at or below ``max_pool_size``. When more fresh valid
configs arrive than there are free slots, an equal number of *randomly chosen*
existing active configs are evicted to make room (true random selection via
:func:`random.sample`).
"""
from __future__ import annotations

import random

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_manager import settings
from app.models import Config, utcnow


async def active_count(session: AsyncSession) -> int:
    return int(
        (await session.execute(
            select(func.count()).select_from(Config).where(Config.active.is_(True))
        )).scalar_one()
    )


async def archive_count(session: AsyncSession) -> int:
    return int((await session.execute(select(func.count()).select_from(Config))).scalar_one())


async def _active_rows(session: AsyncSession) -> list[Config]:
    return list(
        (await session.execute(select(Config).where(Config.active.is_(True)))).scalars().all()
    )


async def add_to_pool(session: AsyncSession, candidates: list[Config]) -> tuple[int, int]:
    """Promote ``candidates`` into the active pool, evicting at random if full.

    Returns ``(added, removed)``.
    """
    max_size = settings.max_pool_size
    if max_size <= 0 or not candidates:
        return 0, 0

    # Never try to add more than the pool can hold.
    to_add = candidates[:max_size]
    current = await active_count(session)
    free = max_size - current
    removed = 0

    if len(to_add) > free:
        need_remove = min(current, len(to_add) - free)
        if need_remove > 0:
            victims = random.sample(await _active_rows(session), need_remove)
            for v in victims:
                v.active = False
            removed = need_remove

    for cfg in to_add:
        cfg.active = True
        cfg.alive = True
        cfg.last_seen = utcnow()

    return len(to_add), removed


async def evict(session: AsyncSession, fingerprint: str) -> bool:
    row = (await session.execute(
        select(Config).where(Config.fingerprint == fingerprint)
    )).scalar_one_or_none()
    if row and row.active:
        row.active = False
        return True
    return False


async def trim_to_size(session: AsyncSession) -> int:
    """Enforce the pool ceiling (e.g. after the user lowers max size)."""
    rows = await _active_rows(session)
    over = len(rows) - settings.max_pool_size
    if over <= 0:
        return 0
    for v in random.sample(rows, over):
        v.active = False
    return over


async def active_configs(session: AsyncSession) -> list[Config]:
    return list(
        (await session.execute(
            select(Config).where(Config.active.is_(True)).order_by(Config.last_seen.desc())
        )).scalars().all()
    )
