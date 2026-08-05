"""Rotating active-pool management.

The active pool is meant to hold the **newest ``max_pool_size`` healthy
configs** known to v2get. Two rules drive every mutation:

1. Nothing enters the pool unless it passed a TCP health check (``alive``).
2. When room is needed, eviction is *deterministic*, not random:
   dead members go first, then the oldest members (by channel post time, or
   first-seen when the post time is unknown) — even if they are still healthy.

``backfill`` tops the pool back up to the ceiling from the archive whenever a
run leaves free slots (validating each candidate over TCP before promoting it),
so the pool no longer only drains.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import cooldown_manager
from app.core.settings_manager import settings
from app.core.tcp_checker import check_iter
from app.models import Config, utcnow

_EPOCH = datetime(1970, 1, 1)

# Bound the work a single backfill pass may do: it TCP-checks archived rows in
# pages until the pool is full, but never scans more than this many rows.
BACKFILL_SCAN_LIMIT = 1000


def _age(cfg: Config) -> datetime:
    """Recency of a config — channel post time, else first discovery."""
    dt = cfg.posted_at or cfg.first_seen or _EPOCH
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _eviction_order(rows: list[Config]) -> list[Config]:
    """Worst-first: dead before alive, then oldest before newest."""
    return sorted(rows, key=lambda c: (bool(c.alive), _age(c)))


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
    """Promote ``candidates`` into the active pool, evicting the worst if full.

    Only healthy (``alive``) candidates are promoted — a config that failed its
    TCP check never enters the pool. When there are not enough free slots, the
    current members are evicted worst-first: TCP-failed members before healthy
    ones, then oldest before newest.

    Returns ``(added, removed)``.
    """
    max_size = settings.max_pool_size
    if max_size <= 0:
        return 0, 0

    # Health gate + newest-first so the freshest links win any contention.
    to_add = sorted(
        (c for c in candidates if c.alive), key=_age, reverse=True
    )[:max_size]
    if not to_add:
        return 0, 0

    current_rows = await _active_rows(session)
    free = max_size - len(current_rows)
    removed = 0

    if len(to_add) > free:
        need_remove = min(len(current_rows), len(to_add) - free)
        for victim in _eviction_order(current_rows)[:need_remove]:
            victim.active = False
            removed += 1

    for cfg in to_add:
        cfg.active = True
        cfg.alive = True
        cfg.last_seen = utcnow()

    return len(to_add), removed


async def backfill(session: AsyncSession) -> tuple[int, int]:
    """Refill free pool slots from the newest archived configs.

    Candidates are taken newest-first from rows that are not active and not in
    cooldown, TCP-checked, and only promoted when the check passes. Returns
    ``(promoted, checked)``.
    """
    max_size = settings.max_pool_size
    free = max_size - await active_count(session)
    if free <= 0:
        return 0, 0

    promoted = 0
    checked = 0
    scanned = 0
    page = max(free * 3, 50)

    while free > 0 and scanned < BACKFILL_SCAN_LIMIT:
        rows = list((await session.execute(
            select(Config)
            .where(Config.active.is_(False))
            .order_by(Config.posted_at.desc(), Config.first_seen.desc())
            .offset(scanned)
            .limit(page)
        )).scalars().all())
        if not rows:
            break
        scanned += len(rows)

        candidates: list[Config] = []
        for row in rows:
            if await cooldown_manager.is_in_cooldown(session, row.fingerprint):
                continue
            candidates.append(row)
        if not candidates:
            continue

        results = await check_iter(
            candidates, key=lambda c: (c.host, c.port),
            timeout=settings.tcp_timeout, concurrency=settings.tcp_concurrency,
        )
        checked += len(candidates)

        for row, alive in zip(candidates, results):
            row.last_checked = utcnow()
            if not alive:
                row.alive = False
                await cooldown_manager.record_failure(session, row.fingerprint)
                continue
            row.alive = True
            await cooldown_manager.record_success(session, row.fingerprint)
            if free > 0:
                row.active = True
                row.last_seen = utcnow()
                promoted += 1
                free -= 1

    return promoted, checked


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
    for victim in _eviction_order(rows)[:over]:
        victim.active = False
    return over


async def active_configs(session: AsyncSession) -> list[Config]:
    return list(
        (await session.execute(
            select(Config).where(Config.active.is_(True)).order_by(Config.last_seen.desc())
        )).scalars().all()
    )
