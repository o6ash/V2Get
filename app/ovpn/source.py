"""Discover .ovpn document attachments in the monitored channels.

Reads the same enabled channels the core collector uses, but only looks at
message *documents* with an ``.ovpn`` extension. Cursors are private to this
module (``ovpn_channel_cursors``) so advancing them never affects the core
collector nor the npvt module.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from app.core.logbook import get_logger
from app.core.telegram_client import collector_client
from app.database import SessionLocal
from app.models import Channel
from app.ovpn.config import ovpn_settings
from app.ovpn.models import OvpnChannelCursor

log = get_logger()

_OVPN_EXT = ".ovpn"


@dataclass(slots=True)
class OvpnCandidate:
    channel: str
    message_id: int
    file_name: str
    file_size: int


def _is_ovpn(msg) -> tuple[bool, str, int]:
    f = getattr(msg, "file", None)
    if not f:
        return False, "", 0
    name = getattr(f, "name", "") or ""
    ext = (getattr(f, "ext", "") or "").lower()
    size = getattr(f, "size", 0) or 0
    ok = ext == _OVPN_EXT or name.lower().endswith(_OVPN_EXT)
    return ok, name or f"message_{getattr(msg, 'id', 0)}.ovpn", size


async def discover() -> list[OvpnCandidate]:
    """Scan enabled channels for fresh .ovpn attachments. Never raises."""
    client = await collector_client._ensure_client()
    if client is None:
        log.debug("ovpn: discovery skipped — Telegram session unavailable")
        return []

    limit = ovpn_settings.scan_message_limit
    candidates: list[OvpnCandidate] = []

    async with SessionLocal() as session:
        channels = (await session.execute(
            select(Channel).where(Channel.enabled.is_(True))
        )).scalars().all()

        for channel in channels:
            cursor = await session.get(OvpnChannelCursor, channel.username)
            if cursor is None:
                cursor = OvpnChannelCursor(username=channel.username, last_message_id=0)
                session.add(cursor)
            last_id = cursor.last_message_id
            max_seen = last_id

            try:
                entity = await client.get_entity(channel.username)
                async for msg in client.iter_messages(entity, min_id=last_id, limit=limit):
                    max_seen = max(max_seen, msg.id)
                    ok, name, size = _is_ovpn(msg)
                    if ok:
                        candidates.append(OvpnCandidate(
                            channel=channel.username, message_id=msg.id,
                            file_name=name, file_size=size,
                        ))
            except Exception as exc:  # noqa: BLE001 - many telethon error types
                log.warning("ovpn: discovery error on %s: %s", channel.username, exc)

            if max_seen > cursor.last_message_id:
                cursor.last_message_id = max_seen

        await session.commit()

    if candidates:
        log.info("ovpn: discovered %d new .ovpn file(s)", len(candidates))
    return candidates


async def download_by_id(channel: str, message_id: int, max_bytes: int) -> bytes | None:
    """Re-fetch a message by id and download its document, honouring the cap."""
    client = await collector_client._ensure_client()
    if client is None:
        return None
    try:
        entity = await client.get_entity(channel)
        msg = await client.get_messages(entity, ids=message_id)
        if msg is None or not getattr(msg, "file", None):
            log.warning("ovpn: message %s in %s no longer has a document", message_id, channel)
            return None
        size = getattr(msg.file, "size", 0) or 0
        if max_bytes and size and size > max_bytes:
            log.warning("ovpn: %s/%s skipped — %d bytes exceeds cap %d",
                        channel, message_id, size, max_bytes)
            return None
        data = await client.download_media(msg, file=bytes)
        return data or None
    except Exception as exc:  # noqa: BLE001
        log.warning("ovpn: download failed for %s/%s: %s", channel, message_id, exc)
        return None
