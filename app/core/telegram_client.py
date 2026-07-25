"""Telegram collection via Telethon.

Uses a logged-in *user* StringSession (bots cannot read arbitrary public
channel history). If credentials are absent the client degrades gracefully:
collection runs report zero messages instead of crashing, so the rest of the
platform — dashboard, pool, outputs — stays fully functional.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.config import config
from app.core.logbook import get_logger

log = get_logger()

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    _TELETHON = True
except ImportError:  # pragma: no cover
    _TELETHON = False


@dataclass(slots=True)
class FetchedMessage:
    id: int
    text: str
    date: datetime | None = None  # channel post time (UTC, tz-aware) from Telegram


class TelegramCollector:
    def __init__(self) -> None:
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(
            _TELETHON
            and config.telegram_api_id
            and config.telegram_api_hash
            and config.telegram_session
        )

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        if not self.configured:
            return None
        self._client = TelegramClient(
            StringSession(config.telegram_session),
            config.telegram_api_id,
            config.telegram_api_hash,
        )
        await self._client.connect()
        if not await self._client.is_user_authorized():
            log.error("Telegram session is not authorized — check TELEGRAM_SESSION")
            await self._client.disconnect()
            self._client = None
            return None
        return self._client

    async def fetch_new(self, username: str, last_id: int, limit: int = 200) -> list[FetchedMessage]:
        """Return messages newer than ``last_id`` for ``username`` (oldest first)."""
        client = await self._ensure_client()
        if client is None:
            return []
        try:
            entity = await client.get_entity(username)
        except Exception as exc:  # noqa: BLE001 - many Telethon error types
            log.warning("Telegram: cannot resolve channel %s: %s", username, exc)
            return []

        out: list[FetchedMessage] = []
        try:
            # Fetch the most RECENT ``limit`` messages newer than ``last_id``
            # (Telethon's default order is newest-first). So "scan last N" means
            # the N latest messages — NOT the oldest N still in the backlog,
            # which would replay months-old history as if it were new.
            async for msg in client.iter_messages(entity, min_id=last_id, limit=limit):
                out.append(FetchedMessage(
                    id=msg.id, text=msg.message or "", date=msg.date))
        except Exception as exc:  # noqa: BLE001
            log.warning("Telegram: error reading %s: %s", username, exc)
        out.reverse()  # hand back oldest-first for stable processing/cursor update
        return out

    async def resolve_title(self, username: str) -> str:
        client = await self._ensure_client()
        if client is None:
            return ""
        try:
            entity = await client.get_entity(username)
            return getattr(entity, "title", "") or getattr(entity, "username", "")
        except Exception:  # noqa: BLE001
            return ""

    async def close(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._client = None


collector_client = TelegramCollector()
