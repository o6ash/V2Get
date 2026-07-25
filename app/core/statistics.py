"""Aggregate statistics for the dashboard."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import cooldown_manager, pool_manager
from app.models import Channel, Config, StatPoint


async def overview(session: AsyncSession) -> dict:
    active = await pool_manager.active_count(session)
    archive = await pool_manager.archive_count(session)
    cooldowns = await cooldown_manager.active_cooldown_count(session)

    alive = int((await session.execute(
        select(func.count()).select_from(Config).where(Config.alive.is_(True))
    )).scalar_one())
    checked_failed = int((await session.execute(
        select(func.count()).select_from(Config)
        .where(Config.last_checked.isnot(None), Config.alive.is_(False))
    )).scalar_one())

    total_checked = alive + checked_failed
    success_rate = round(100 * alive / total_checked, 1) if total_checked else 0.0

    by_protocol = dict(
        (await session.execute(
            select(Config.protocol, func.count()).group_by(Config.protocol)
        )).all()
    )

    return {
        "active_count": active,
        "archive_count": archive,
        "cooldown_count": cooldowns,
        "alive_count": alive,
        "failed_count": checked_failed,
        "success_rate": success_rate,
        "by_protocol": by_protocol,
    }


async def channel_performance(session: AsyncSession) -> list[dict]:
    rows = (await session.execute(select(Channel))).scalars().all()
    out: list[dict] = []
    for c in rows:
        found = c.configs_found or 0
        acceptance = round(100 * c.configs_accepted / found, 1) if found else 0.0
        dup_rate = round(100 * c.duplicates_removed / found, 1) if found else 0.0
        out.append({
            "id": c.id,
            "username": c.username,
            "title": c.title,
            "enabled": c.enabled,
            "last_message_id": c.last_message_id,
            "scan_limit": c.scan_limit,
            "messages_scanned": c.messages_scanned,
            "configs_found": found,
            "configs_accepted": c.configs_accepted,
            "duplicates_removed": c.duplicates_removed,
            "acceptance_rate": acceptance,
            "duplicate_rate": dup_rate,
        })
    return out


async def duplicate_stats(session: AsyncSession) -> dict:
    total_found = int((await session.execute(
        select(func.coalesce(func.sum(Channel.configs_found), 0))
    )).scalar_one())
    total_dupes = int((await session.execute(
        select(func.coalesce(func.sum(Channel.duplicates_removed), 0))
    )).scalar_one())
    ratio = round(100 * total_dupes / total_found, 1) if total_found else 0.0
    return {"total_duplicates_removed": total_dupes, "duplicate_ratio": ratio}


async def record_snapshot(session: AsyncSession) -> None:
    ov = await overview(session)
    session.add(StatPoint(
        active_count=ov["active_count"],
        archive_count=ov["archive_count"],
        cooldown_count=ov["cooldown_count"],
        alive_rate=ov["success_rate"],
    ))
    # Trim history to the configured retention.
    from app.core.settings_manager import settings
    keep = settings.get("stat_retention_points", 2000)
    ids = (await session.execute(
        select(StatPoint.id).order_by(StatPoint.id.desc()).offset(keep)
    )).scalars().all()
    if ids:
        from sqlalchemy import delete
        await session.execute(delete(StatPoint).where(StatPoint.id.in_(ids)))


async def history(session: AsyncSession, limit: int = 200) -> list[dict]:
    rows = (await session.execute(
        select(StatPoint).order_by(StatPoint.id.desc()).limit(limit)
    )).scalars().all()
    return [
        {
            "ts": r.ts.isoformat() if r.ts else None,
            "active": r.active_count,
            "archive": r.archive_count,
            "cooldown": r.cooldown_count,
            "alive_rate": r.alive_rate,
        }
        for r in reversed(rows)
    ]
