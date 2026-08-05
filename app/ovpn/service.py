"""Public facade for the ovpn module — the single object the app wires in."""
from __future__ import annotations

from typing import Any

from sqlalchemy import desc, func, select

from app.core.logbook import get_logger
from app.database import SessionLocal
from app.ovpn import models, publisher
from app.ovpn.config import DEFAULTS, ovpn_settings
from app.ovpn.models import OvpnFile
from app.ovpn.worker import worker

log = get_logger()


class OvpnService:
    def __init__(self) -> None:
        self._started = False

    async def start(self) -> None:
        """Init tables, load settings, launch the worker. Never blocks startup."""
        if self._started:
            return
        try:
            await models.init_models()
            await ovpn_settings.load()
            await worker.start()
            self._started = True
            log.info("ovpn service ready (collection=%s publish=%s)",
                     ovpn_settings.collection_enabled, ovpn_settings.publish_enabled)
        except Exception as exc:  # noqa: BLE001 - must never block app startup
            log.exception("ovpn service failed to start (feature disabled): %s", exc)

    async def stop(self) -> None:
        if not self._started:
            return
        try:
            await worker.stop()
        finally:
            self._started = False

    # ── settings ─────────────────────────────────────────────────────────────
    def settings(self) -> dict[str, Any]:
        return ovpn_settings.all()

    async def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        result = await ovpn_settings.update(values)
        worker.trigger_scan()
        return result

    # ── state / actions ──────────────────────────────────────────────────────
    async def state(self) -> dict[str, Any]:
        async with SessionLocal() as session:
            total = int((await session.execute(
                select(func.count()).select_from(OvpnFile)
            )).scalar_one())
            by_status = dict((await session.execute(
                select(OvpnFile.status, func.count()).group_by(OvpnFile.status)
            )).all())
            healthy = int((await session.execute(
                select(func.count()).select_from(OvpnFile).where(OvpnFile.healthy.is_(True))
            )).scalar_one())
        return {
            "started": self._started,
            "toggles": {
                "collection_enabled": ovpn_settings.collection_enabled,
                "publish_enabled": ovpn_settings.publish_enabled,
            },
            "files_total": total,
            "healthy_total": healthy,
            "by_status": by_status,
            "subscription_url": publisher.index_url(),
            "stats": dict(worker.stats),
        }

    async def recent_files(self, limit: int = 100) -> list[dict[str, Any]]:
        async with SessionLocal() as session:
            rows = (await session.execute(
                select(OvpnFile).order_by(desc(OvpnFile.id)).limit(limit)
            )).scalars().all()
        return [
            {
                "id": r.id,
                "source_channel": r.source_channel,
                "source_message_id": r.source_message_id,
                "file_name": r.file_name,
                "file_size": r.file_size,
                "remote": f"{r.remote_host}:{r.remote_port}/{r.remote_proto}"
                          if r.remote_host else "",
                "healthy": r.healthy,
                "status": r.status,
                "published": r.published,
                "stored_name": r.stored_name,
                "error": r.error,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "checked_at": r.checked_at.isoformat() if r.checked_at else None,
            }
            for r in rows
        ]

    def trigger_scan(self) -> None:
        worker.trigger_scan()

    @staticmethod
    def defaults() -> dict[str, Any]:
        return dict(DEFAULTS)


service = OvpnService()
