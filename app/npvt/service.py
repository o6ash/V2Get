"""Public facade for the npvt module — the single object the app wires in.

Owns startup/shutdown of the background worker, exposes a consolidated state
snapshot for the dashboard, and proxies settings reads/writes. Importing this
module has no side effects beyond constructing the singleton; the worker only
runs after :meth:`NpvtService.start` is called from the app lifespan.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import desc, func, select

from app.core.logbook import get_logger
from app.database import SessionLocal
from app.npvt import models
from app.npvt.config import DEFAULTS, npvt_settings
from app.npvt.models import NpvtFile
from app.npvt.worker import worker

log = get_logger()


class NpvtService:
    def __init__(self) -> None:
        self._started = False

    async def start(self) -> None:
        """Initialise tables, load settings and launch the worker. Fail-safe."""
        if self._started:
            return
        try:
            await models.init_models()
            await npvt_settings.load()
            await worker.start()
            self._started = True
            log.info("npvt service ready (collection=%s links=%s)",
                     npvt_settings.collection_enabled,
                     npvt_settings.link_collection_enabled)
        except Exception as exc:  # noqa: BLE001 - must never block app startup
            log.exception("npvt service failed to start (feature disabled): %s", exc)

    async def stop(self) -> None:
        if not self._started:
            return
        try:
            await worker.stop()
        finally:
            self._started = False

    # ── settings ───────────────────────────────────────────────────────────--
    def settings(self) -> dict[str, Any]:
        return npvt_settings.all()

    async def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        before = npvt_settings.unlock_concurrency
        result = await npvt_settings.update(values)
        # Wake discovery so toggles take effect promptly.
        worker.trigger_scan()
        if npvt_settings.unlock_concurrency != before:
            log.info("npvt: unlock_concurrency changed — applies on next restart")
        return result

    # ── state / actions ──────────────────────────────────────────────────────
    async def state(self) -> dict[str, Any]:
        async with SessionLocal() as session:
            total = int((await session.execute(
                select(func.count()).select_from(NpvtFile)
            )).scalar_one())
            by_status = dict((await session.execute(
                select(NpvtFile.status, func.count()).group_by(NpvtFile.status)
            )).all())
        return {
            "started": self._started,
            "toggles": {
                "collection_enabled": npvt_settings.collection_enabled,
                "link_collection_enabled": npvt_settings.link_collection_enabled,
            },
            # Unlocking runs in-process; no external service to report on.
            "unlock_mode": "local",
            "queue_size": worker.queue_size,
            "files_total": total,
            "by_status": by_status,
            "stats": dict(worker.stats),
        }

    async def recent_files(self, limit: int = 100) -> list[dict[str, Any]]:
        async with SessionLocal() as session:
            rows = (await session.execute(
                select(NpvtFile).order_by(desc(NpvtFile.id)).limit(limit)
            )).scalars().all()
        return [
            {
                "id": r.id,
                "source_channel": r.source_channel,
                "source_message_id": r.source_message_id,
                "file_name": r.file_name,
                "file_size": r.file_size,
                "status": r.status,
                "attempts": r.attempts,
                "links_found": r.links_found,
                "links_injected": r.links_injected,
                "error": r.error,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]

    def trigger_scan(self) -> None:
        worker.trigger_scan()

    async def retry_file(self, file_id: int) -> bool:
        return await worker.retry_file(file_id)

    async def clear_queue(self) -> int:
        """Cancel + delete all pending/processing files. Returns deleted count."""
        return await worker.clear_queue()

    @staticmethod
    def defaults() -> dict[str, Any]:
        return dict(DEFAULTS)


service = NpvtService()
