"""REST API consumed by the dashboard."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import (
    cooldown_manager,
    github_sync,
    pool_manager,
    statistics,
    subscription,
)
from app.core.blacklist import blacklist
from app.core.collector import run_once
from app.core.logbook import read_log_file, ring, search_log_file
from app.core.scheduler import scheduler
from app.core.settings_manager import settings
from app.core.telegram_client import collector_client
from app.database import get_session
from app.models import Channel, Config, RunLog
from app.schemas import (
    BlacklistEntry,
    BlacklistReplace,
    ChannelCreate,
    ChannelUpdate,
    FingerprintRef,
    SettingsUpdate,
)

router = APIRouter(prefix="/api")


# ── overview ──────────────────────────────────────────────────────────────────
# NB: /api/health is intentionally defined unauthenticated in app.main so the
# container healthcheck (and external monitors) can probe liveness without creds.
@router.get("/overview")
async def overview(session: AsyncSession = Depends(get_session)) -> dict:
    stats = await statistics.overview(session)
    dupes = await statistics.duplicate_stats(session)
    gh = await github_sync.get_status(session)
    return {
        "scheduler": scheduler.state(),
        "stats": stats,
        "duplicates": dupes,
        "github": gh,
        "telegram_configured": collector_client.configured,
    }


@router.post("/run")
async def manual_run() -> dict:
    if scheduler.running:
        return {"status": "already_running"}
    scheduler.trigger_now()
    return {"status": "triggered"}


# ── channels ─────────────────────────────────────────────────────────────────--
@router.get("/channels")
async def list_channels(session: AsyncSession = Depends(get_session)) -> list[dict]:
    return await statistics.channel_performance(session)


@router.post("/channels")
async def add_channel(payload: ChannelCreate, session: AsyncSession = Depends(get_session)) -> dict:
    username = payload.username.strip().lstrip("@")
    username = username.replace("https://t.me/", "").replace("t.me/", "").strip("/")
    if not username:
        raise HTTPException(400, "Invalid channel username")
    existing = (await session.execute(
        select(Channel).where(Channel.username == username)
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(409, "Channel already exists")
    title = await collector_client.resolve_title(username)
    channel = Channel(username=username, title=title)
    if payload.scan_limit is not None:
        channel.scan_limit = payload.scan_limit
    session.add(channel)
    await session.commit()
    return {
        "id": channel.id, "username": channel.username,
        "title": channel.title, "scan_limit": channel.scan_limit,
    }


@router.patch("/channels/{channel_id}")
async def update_channel(
    channel_id: int, payload: ChannelUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    channel = await session.get(Channel, channel_id)
    if not channel:
        raise HTTPException(404, "Channel not found")
    if payload.enabled is not None:
        channel.enabled = payload.enabled
    if payload.last_message_id is not None:
        channel.last_message_id = payload.last_message_id
    if payload.scan_limit is not None:
        channel.scan_limit = payload.scan_limit
    await session.commit()
    return {
        "id": channel.id, "enabled": channel.enabled,
        "last_message_id": channel.last_message_id, "scan_limit": channel.scan_limit,
    }


@router.delete("/channels/{channel_id}")
async def delete_channel(channel_id: int, session: AsyncSession = Depends(get_session)) -> dict:
    channel = await session.get(Channel, channel_id)
    if not channel:
        raise HTTPException(404, "Channel not found")
    await session.delete(channel)
    await session.commit()
    return {"deleted": channel_id}


# ── active pool ─────────────────────────────────────────────────────────────---
def _config_dict(c: Config) -> dict:
    return {
        "fingerprint": c.fingerprint,
        "protocol": c.protocol,
        "name": c.name,
        "host": c.host,
        "port": c.port,
        "alive": c.alive,
        "active": c.active,
        "source_channel": c.source_channel,
        "first_seen": c.first_seen.isoformat() if c.first_seen else None,
        "last_seen": c.last_seen.isoformat() if c.last_seen else None,
        "raw": c.raw,
    }


def _filtered_query(active_only: bool, search: str | None, protocol: str | None):
    stmt = select(Config)
    if active_only:
        stmt = stmt.where(Config.active.is_(True))
    if protocol:
        stmt = stmt.where(Config.protocol == protocol)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(or_(
            Config.host.ilike(like), Config.name.ilike(like), Config.raw.ilike(like)
        ))
    return stmt


@router.get("/active")
async def list_active(
    search: str | None = None,
    protocol: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    stmt = _filtered_query(True, search, protocol).order_by(Config.last_seen.desc())
    rows = (await session.execute(stmt)).scalars().all()
    return [_config_dict(c) for c in rows]


@router.delete("/active/{fingerprint}")
async def delete_active(fingerprint: str, session: AsyncSession = Depends(get_session)) -> dict:
    removed = await pool_manager.evict(session, fingerprint)
    if removed:
        await subscription.generate(session)
    await session.commit()
    return {"evicted": removed}


@router.get("/active/export", response_class=PlainTextResponse)
async def export_active(session: AsyncSession = Depends(get_session)) -> str:
    rows = (await session.execute(
        select(Config.raw).where(Config.active.is_(True))
    )).scalars().all()
    return "\n".join(rows)


# ── archive ─────────────────────────────────────────────────────────────────---
@router.get("/archive")
async def list_archive(
    search: str | None = None,
    protocol: str | None = None,
    limit: int = Query(200, le=2000),
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> dict:
    base = _filtered_query(False, search, protocol)
    total = int((await session.execute(
        select(func.count()).select_from(base.subquery())
    )).scalar_one())
    rows = (await session.execute(
        base.order_by(Config.last_seen.desc()).limit(limit).offset(offset)
    )).scalars().all()
    return {"total": total, "items": [_config_dict(c) for c in rows]}


@router.get("/archive/export", response_class=PlainTextResponse)
async def export_archive(session: AsyncSession = Depends(get_session)) -> str:
    rows = (await session.execute(select(Config.raw))).scalars().all()
    return "\n".join(rows)


@router.post("/archive/cleanup")
async def cleanup_archive(
    keep_active: bool = True, session: AsyncSession = Depends(get_session)
) -> dict:
    """Remove archived (non-active) configs. Active pool is preserved by default."""
    stmt = delete(Config)
    if keep_active:
        stmt = stmt.where(Config.active.is_(False))
    result = await session.execute(stmt)
    await session.commit()
    await subscription.generate(session)
    return {"deleted": result.rowcount or 0}


# ── cooldown ─────────────────────────────────────────────────────────────────--
@router.get("/cooldown")
async def list_cooldown(
    active_only: bool = True, session: AsyncSession = Depends(get_session)
) -> list[dict]:
    return await cooldown_manager.list_cooldowns(session, active_only=active_only)


@router.post("/cooldown/remove")
async def remove_cooldown(ref: FingerprintRef, session: AsyncSession = Depends(get_session)) -> dict:
    await cooldown_manager.remove_cooldown(session, ref.fingerprint)
    await session.commit()
    return {"removed": ref.fingerprint}


@router.post("/cooldown/reset")
async def reset_cooldown(ref: FingerprintRef, session: AsyncSession = Depends(get_session)) -> dict:
    await cooldown_manager.reset_fail_count(session, ref.fingerprint)
    await session.commit()
    return {"reset": ref.fingerprint}


# ── blacklist ────────────────────────────────────────────────────────────────--
@router.get("/blacklist")
async def get_blacklist() -> dict:
    return blacklist.all()


@router.post("/blacklist/add")
async def add_blacklist(entry: BlacklistEntry) -> dict:
    blacklist.add(entry.kind, entry.entry)
    return {"kind": entry.kind, "entries": blacklist.get(entry.kind)}


@router.post("/blacklist/remove")
async def remove_blacklist(entry: BlacklistEntry) -> dict:
    blacklist.remove(entry.kind, entry.entry)
    return {"kind": entry.kind, "entries": blacklist.get(entry.kind)}


@router.post("/blacklist/replace")
async def replace_blacklist(payload: BlacklistReplace) -> dict:
    blacklist.replace(payload.kind, payload.entries)
    return {"kind": payload.kind, "entries": blacklist.get(payload.kind)}


@router.get("/blacklist/export/{kind}", response_class=PlainTextResponse)
async def export_blacklist(kind: str) -> str:
    if kind not in ("domains", "ips", "keywords"):
        raise HTTPException(400, "invalid kind")
    return "\n".join(blacklist.get(kind))


# ── logs ─────────────────────────────────────────────────────────────────────--
@router.get("/logs")
async def get_logs(tail: int = Query(200, le=1000), q: str | None = None) -> dict:
    return {"lines": ring.tail(tail, q)}


@router.get("/logs/search")
async def logs_search(q: str, limit: int = Query(500, le=2000)) -> dict:
    return {"lines": search_log_file(q, limit)}


@router.get("/logs/download", response_class=PlainTextResponse)
async def download_logs() -> str:
    return read_log_file()


@router.get("/runs")
async def recent_runs(limit: int = Query(20, le=200), session: AsyncSession = Depends(get_session)) -> list[dict]:
    rows = (await session.execute(
        select(RunLog).order_by(RunLog.id.desc()).limit(limit)
    )).scalars().all()
    return [
        {
            "id": r.id,
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "channels_scanned": r.channels_scanned,
            "messages_read": r.messages_read,
            "configs_found": r.configs_found,
            "duplicates_removed": r.duplicates_removed,
            "tcp_failed": r.tcp_failed,
            "cooldown_skipped": r.cooldown_skipped,
            "added": r.added,
            "removed": r.removed,
            "active_pool": r.active_pool,
            "github_push": r.github_push,
            "ok": r.ok,
        }
        for r in rows
    ]


# ── settings ─────────────────────────────────────────────────────────────────--
@router.get("/settings")
async def get_settings() -> dict:
    return settings.public()


@router.put("/settings")
async def update_settings(payload: SettingsUpdate, session: AsyncSession = Depends(get_session)) -> dict:
    await settings.update(payload.values)
    # Enforce pool ceiling immediately if it was lowered.
    if await pool_manager.trim_to_size(session):
        await subscription.generate(session)
        await session.commit()
    return settings.public()


# ── statistics ───────────────────────────────────────────────────────────────--
@router.get("/stats/channels")
async def stats_channels(session: AsyncSession = Depends(get_session)) -> list[dict]:
    return await statistics.channel_performance(session)


@router.get("/stats/history")
async def stats_history(limit: int = Query(200, le=2000), session: AsyncSession = Depends(get_session)) -> list[dict]:
    return await statistics.history(session, limit)


# ── github & outputs ─────────────────────────────────────────────────────────--
@router.get("/github")
async def github_status(session: AsyncSession = Depends(get_session)) -> dict:
    return await github_sync.get_status(session)


@router.get("/output")
async def list_output() -> dict:
    return {"files": subscription.published_files() + [subscription.ARCHIVE_FILE]}


@router.get("/output/{name}", response_class=PlainTextResponse)
async def preview_output(name: str) -> str:
    content = subscription.read_output(name)
    if content is None:
        raise HTTPException(404, "output not generated yet")
    # Cap preview size to keep the UI responsive.
    return content[:200_000]
