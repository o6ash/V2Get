"""Inject bot-returned V2Ray links into the existing v2get pipeline.

This reuses the core building blocks exactly as the collector does — parser,
fingerprint, blacklist, cooldown, pool rotation, subscription generation and
GitHub publishing — without importing or altering the collector's run loop. The
only coupling is the collector's module-level ``_run_lock``: we acquire it so an
npvt injection and a scheduled collection run never mutate the active pool
concurrently. The lock is held only for the (short, bounded) DB/TCP/pool
critical section — never during the network-bound bot relay.
"""
from __future__ import annotations

import hashlib

from sqlalchemy import select

from app.core import github_sync, pool_manager, statistics, subscription
from app.core import cooldown_manager
from app.core.blacklist import blacklist
from app.core.fingerprint import fingerprint
from app.core.logbook import get_logger
from app.core.parser import parse_link, parse_text
from app.core.settings_manager import settings
from app.core.tcp_checker import check_iter
from app.database import SessionLocal
from app.models import Config, utcnow
from app.npvt.config import npvt_settings
from app.npvt.models import NpvtLink

log = get_logger()


def _link_hash(raw: str) -> str:
    return hashlib.sha256(raw.strip().encode("utf-8")).hexdigest()


async def inject(raw_links: list[str], source: str, file_id: int | None = None) -> dict:
    """Run ``raw_links`` through the full pipeline. Returns a summary dict.

    ``source`` is recorded as the config's ``source_channel`` for provenance.
    Safe to call repeatedly; fingerprint + raw-link dedup keep it idempotent.
    """
    summary = {
        "received": len(raw_links), "parsed": 0, "new": 0, "refreshed": 0,
        "duplicates": 0, "blocked": 0, "cooldown_skipped": 0,
        "alive": 0, "added": 0, "removed": 0, "github": "skipped",
    }
    if not raw_links:
        return summary

    # Serialise with the core collector so pool mutations never overlap. Import
    # locally to avoid any import-time coupling with the collector module.
    from app.core import collector

    async with collector._run_lock:
        async with SessionLocal() as session:
            seen: set[str] = set()
            fresh: list[Config] = []

            for raw in raw_links:
                cfg = parse_link(raw) or next(iter(parse_text(raw)), None)
                if not cfg or not cfg.valid:
                    continue
                summary["parsed"] += 1

                # Module-level raw-link dedup (provenance + avoid rework).
                lh = _link_hash(cfg.raw)
                link_row = (await session.execute(
                    select(NpvtLink).where(NpvtLink.link_hash == lh)
                )).scalar_one_or_none()
                if link_row is None:
                    link_row = NpvtLink(
                        link_hash=lh, raw=cfg.raw, protocol=cfg.protocol,
                        file_id=file_id, injected=False,
                    )
                    session.add(link_row)

                blocked, _reason = blacklist.is_blocked(cfg)
                if blocked:
                    summary["blocked"] += 1
                    continue

                fp = fingerprint(cfg)
                if fp in seen:
                    summary["duplicates"] += 1
                    continue
                seen.add(fp)

                if await cooldown_manager.is_in_cooldown(session, fp):
                    summary["cooldown_skipped"] += 1
                    continue

                existing = (await session.execute(
                    select(Config).where(Config.fingerprint == fp)
                )).scalar_one_or_none()

                if existing:
                    existing.last_seen = utcnow()
                    # Preserve the original source — don't overwrite the origin.
                    if not existing.source_channel:
                        existing.source_channel = source
                    summary["refreshed"] += 1
                    if not existing.active:
                        fresh.append(existing)
                else:
                    row = Config(
                        fingerprint=fp, protocol=cfg.protocol, raw=cfg.raw,
                        name=cfg.name, host=cfg.host, port=cfg.port,
                        source_channel=source,
                    )
                    session.add(row)
                    await session.flush()
                    summary["new"] += 1
                    fresh.append(row)

                # Mark the raw link as injected (regardless of new/refresh).
                link_row.injected = True

            # ── TCP validation of the fresh candidates ─────────────────────────
            if fresh:
                results = await check_iter(
                    fresh, key=lambda c: (c.host, c.port),
                    timeout=settings.tcp_timeout, concurrency=settings.tcp_concurrency,
                )
                for row, alive in zip(fresh, results):
                    row.last_checked = utcnow()
                    if alive:
                        row.alive = True
                        summary["alive"] += 1
                        await cooldown_manager.record_success(session, row.fingerprint)
                    else:
                        row.alive = False
                        await cooldown_manager.record_failure(session, row.fingerprint)

            # ── Pool rotation (reuse the exact core logic) ─────────────────────
            candidates = [r for r in fresh if r.alive and not r.active]
            added, removed = await pool_manager.add_to_pool(session, candidates)
            summary["added"], summary["removed"] = added, removed

            await session.flush()

            # ── Outputs + publishing ───────────────────────────────────────────
            if npvt_settings.publish_after_ingest:
                files = await subscription.generate(session)
                published = {n: c for n, c in files.items()
                             if n in subscription.published_files()}
                push = await github_sync.publish(session, published)
                summary["github"] = push.get("status", "skipped")
                await statistics.record_snapshot(session)

            await session.commit()

    log.info(
        "npvt ingest [%s]: parsed=%d new=%d refreshed=%d alive=%d added=%d removed=%d github=%s",
        source, summary["parsed"], summary["new"], summary["refreshed"],
        summary["alive"], summary["added"], summary["removed"], summary["github"],
    )
    return summary
