"""Orchestrates a single end-to-end collection run.

    collect → parse → dedup/blacklist/cooldown filter → TCP validate →
    cooldown bookkeeping → pool rotation → outputs → GitHub publish → stats

A module-level lock guarantees runs never overlap (the scheduler and the manual
dashboard button share it).
"""
from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import (
    cooldown_manager,
    github_sync,
    pool_manager,
    statistics,
    subscription,
)
from app.core.blacklist import blacklist
from app.core.fingerprint import fingerprint
from app.core.logbook import get_logger, log_run_summary
from app.core.parser import parse_text
from app.core.settings_manager import settings
from app.core.tcp_checker import check_iter
from app.core.telegram_client import collector_client
from app.database import SessionLocal
from app.models import Channel, Config, RunLog, utcnow

log = get_logger()
_run_lock = asyncio.Lock()


def is_running() -> bool:
    return _run_lock.locked()


async def run_once(trigger: str = "scheduled") -> dict:
    """Execute one collection cycle. Returns the run summary dict."""
    if _run_lock.locked():
        log.info("Run requested (%s) but a run is already in progress — skipping", trigger)
        return {"skipped": True, "reason": "already running"}

    async with _run_lock:
        async with SessionLocal() as session:
            run = RunLog(started_at=utcnow())
            session.add(run)
            await session.flush()
            try:
                summary = await _execute(session, run)
                run.ok = True
                await session.commit()
            except Exception as exc:  # noqa: BLE001 - surface but never crash scheduler
                await session.rollback()
                log.exception("Collection run failed: %s", exc)
                async with SessionLocal() as s2:
                    rl = await s2.get(RunLog, run.id)
                    if rl:
                        rl.ok = False
                        rl.finished_at = utcnow()
                        rl.detail = str(exc)
                        await s2.commit()
                return {"ok": False, "error": str(exc), "trigger": trigger}
            summary["trigger"] = trigger
            return summary


async def _execute(session: AsyncSession, run: RunLog) -> dict:
    log.info("── Collection run started ──")
    channels = (await session.execute(
        select(Channel).where(Channel.enabled.is_(True))
    )).scalars().all()

    seen: set[str] = set()
    fresh: list[Config] = []
    messages_read = 0

    # ── Text-link harvesting (optional) ─────────────────────────────────────────
    # With ``collect_text_links`` off the run skips message scanning entirely:
    # no Telegram history is read and no link is taken from channel text. The
    # rest of the run below (TCP re-validation of the active pool, rotation,
    # outputs, GitHub publish, stats) still executes, so a subscription built
    # solely from the .npvt pipeline stays fresh and published.
    collect_text = settings.collect_text_links
    if not collect_text:
        log.info(
            "Text-link collection disabled — skipping channel message scan "
            "(.npvt pipeline unaffected)"
        )

    for channel in (channels if collect_text else ()):
        try:
            messages = await collector_client.fetch_new(
                channel.username, channel.last_message_id,
                limit=max(1, channel.scan_limit or 15),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Channel %s fetch error: %s", channel.username, exc)
            messages = []

        if messages:
            channel.last_message_id = max(channel.last_message_id, *(m.id for m in messages))
        channel.messages_scanned += len(messages)
        messages_read += len(messages)

        for msg in messages:
            for cfg in parse_text(msg.text):
                channel.configs_found += 1
                run.configs_found += 1

                blocked, _reason = blacklist.is_blocked(cfg)
                if blocked:
                    continue

                fp = fingerprint(cfg)
                if fp in seen:
                    channel.duplicates_removed += 1
                    run.duplicates_removed += 1
                    continue
                seen.add(fp)

                if await cooldown_manager.is_in_cooldown(session, fp):
                    run.cooldown_skipped += 1
                    continue

                existing = (await session.execute(
                    select(Config).where(Config.fingerprint == fp)
                )).scalar_one_or_none()

                if existing:
                    existing.last_seen = utcnow()
                    # Keep the config tied to the channel that *first* discovered
                    # it — don't overwrite the origin every time it reappears.
                    if not existing.source_channel:
                        existing.source_channel = channel.username
                    # Backfill the channel post time for rows collected before
                    # we tracked it (or by a path that didn't set it).
                    if existing.posted_at is None and msg.date is not None:
                        existing.posted_at = msg.date
                    channel.duplicates_removed += 1
                    run.duplicates_removed += 1
                    if not existing.active:
                        fresh.append(existing)
                else:
                    row = Config(
                        fingerprint=fp,
                        protocol=cfg.protocol,
                        raw=cfg.raw,
                        name=cfg.name,
                        host=cfg.host,
                        port=cfg.port,
                        source_channel=channel.username,
                        posted_at=msg.date,
                    )
                    session.add(row)
                    await session.flush()
                    channel.configs_accepted += 1
                    fresh.append(row)

    run.channels_scanned = len(channels) if collect_text else 0
    run.messages_read = messages_read

    # ── TCP validation: fresh candidates + the current active pool ──────────────
    active_rows = await pool_manager.active_configs(session)
    fresh_ids = {id(r) for r in fresh}
    to_validate = fresh + [r for r in active_rows if id(r) not in fresh_ids]
    removed = 0

    if to_validate:
        results = await check_iter(
            to_validate,
            key=lambda c: (c.host, c.port),
            timeout=settings.tcp_timeout,
            concurrency=settings.tcp_concurrency,
        )
        for row, alive in zip(to_validate, results):
            row.last_checked = utcnow()
            if alive:
                row.alive = True
                await cooldown_manager.record_success(session, row.fingerprint)
            else:
                row.alive = False
                run.tcp_failed += 1
                tripped = await cooldown_manager.record_failure(session, row.fingerprint)
                if tripped and row.active:
                    row.active = False
                    removed += 1

    # ── Pool rotation ───────────────────────────────────────────────────────────
    candidates = [r for r in fresh if r.alive and not r.active]
    added, evicted = await pool_manager.add_to_pool(session, candidates)
    removed += evicted

    await session.flush()
    run.added = added
    run.removed = removed
    run.active_pool = await pool_manager.active_count(session)

    # ── Outputs + publishing ─────────────────────────────────────────────────────
    files = await subscription.generate(session)
    published = {n: c for n, c in files.items() if n in subscription.published_files()}
    push = await github_sync.publish(session, published)
    run.github_push = push.get("status", "skipped")

    await statistics.record_snapshot(session)
    run.finished_at = utcnow()

    summary = {
        "channels_scanned": run.channels_scanned,
        "messages_read": run.messages_read,
        "configs_found": run.configs_found,
        "duplicates_removed": run.duplicates_removed,
        "tcp_failed": run.tcp_failed,
        "cooldown_skipped": run.cooldown_skipped,
        "added": run.added,
        "removed": run.removed,
        "active_pool": run.active_pool,
        "github_push": run.github_push,
        "ok": True,
    }
    log_run_summary(summary)
    return summary
