"""Discover .npvt document attachments in the monitored channels.

Reads the *same* enabled channels the core collector uses, but looks at message
*documents* (the collector only reads text). Each ``.npvt`` attachment newer
than the per-channel cursor is yielded once; cursors advance so messages are
never rescanned. Downloading is deferred to the worker so size limits and
content-hash dedup are enforced just before relay.
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from app.core.logbook import get_logger
from app.core.telegram_client import collector_client
from app.database import SessionLocal
from app.models import Channel
from app.npvt.config import npvt_settings
from app.npvt.models import NpvtChannelCursor

log = get_logger()

_NPVT_EXT = ".npvt"


@dataclass(slots=True)
class NpvtCandidate:
    channel: str
    message_id: int
    file_name: str
    file_size: int
    message: object  # the Telethon Message, retained so the worker can download it


def _is_npvt(msg) -> tuple[bool, str, int]:
    """Return (is_npvt, file_name, size) for a Telethon message."""
    f = getattr(msg, "file", None)
    if not f:
        return False, "", 0
    name = getattr(f, "name", "") or ""
    ext = (getattr(f, "ext", "") or "").lower()
    size = getattr(f, "size", 0) or 0
    is_npvt = ext == _NPVT_EXT or name.lower().endswith(_NPVT_EXT)
    return is_npvt, name or f"message_{getattr(msg, 'id', 0)}.npvt", size


async def discover() -> list[NpvtCandidate]:
    """Scan enabled channels for fresh .npvt attachments. Never raises."""
    client = await collector_client._ensure_client()
    if client is None:
        log.debug("npvt: discovery skipped — Telegram session unavailable")
        return []

    limit = npvt_settings.scan_message_limit
    candidates: list[NpvtCandidate] = []

    async with SessionLocal() as session:
        channels = (await session.execute(
            select(Channel).where(Channel.enabled.is_(True))
        )).scalars().all()

        for channel in channels:
            cursor = await session.get(NpvtChannelCursor, channel.username)
            if cursor is None:
                cursor = NpvtChannelCursor(
                    username=channel.username, last_message_id=0,
                )
                session.add(cursor)
            last_id = cursor.last_message_id
            max_seen = last_id

            try:
                entity = await client.get_entity(channel.username)
                # Newest-first (no reverse): inspect the most recent ``limit``
                # messages for .npvt attachments, not the oldest of the backlog.
                async for msg in client.iter_messages(
                    entity, min_id=last_id, limit=limit
                ):
                    max_seen = max(max_seen, msg.id)
                    ok, name, size = _is_npvt(msg)
                    if ok:
                        candidates.append(NpvtCandidate(
                            channel=channel.username, message_id=msg.id,
                            file_name=name, file_size=size, message=msg,
                        ))
            except Exception as exc:  # noqa: BLE001 - many telethon error types
                log.warning("npvt: discovery error on %s: %s", channel.username, exc)

            if max_seen > cursor.last_message_id:
                cursor.last_message_id = max_seen

        await session.commit()

    if candidates:
        log.info("npvt: discovered %d new .npvt file(s)", len(candidates))
    return candidates


async def download_by_id(channel: str, message_id: int, max_bytes: int) -> bytes | None:
    """Re-fetch a message by id and download its document, honouring the cap.

    Fetching by id (rather than holding the Telethon ``Message`` in memory)
    makes the worker restart-safe: a file interrupted by a restart can still be
    downloaded from its durable ``(channel, message_id)`` record. None on any
    failure or if the message/document no longer exists or is too large.
    """
    client = await collector_client._ensure_client()
    if client is None:
        return None
    try:
        entity = await client.get_entity(channel)
        msg = await client.get_messages(entity, ids=message_id)
        if msg is None or not getattr(msg, "file", None):
            log.warning("npvt: message %s in %s no longer has a document", message_id, channel)
            return None
        size = getattr(msg.file, "size", 0) or 0
        if max_bytes and size and size > max_bytes:
            log.warning("npvt: %s/%s skipped — %d bytes exceeds cap %d",
                        channel, message_id, size, max_bytes)
            return None
        data = await client.download_media(msg, file=bytes)
        return data or None
    except Exception as exc:  # noqa: BLE001
        log.warning("npvt: download failed for %s/%s: %s", channel, message_id, exc)
        return None
