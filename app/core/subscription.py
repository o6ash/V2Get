"""Generate subscription output files from the active pool and archive.

Always produces ``active.txt`` and ``subscription_base64.txt``. Optionally
produces ``clash.txt`` and ``singbox.txt`` (lightweight passthrough formats).
``archive.txt`` mirrors every unique config ever discovered.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import config
from app.core import clash_export
from app.core.parser import rename
from app.core.settings_manager import settings
from app.models import Config

ACTIVE_FILE = "active.txt"
BASE64_FILE = "subscription_base64.txt"
ARCHIVE_FILE = "archive.txt"
CLASH_FILE = "clash.txt"
STASH_FILE = "stash.txt"
SINGBOX_FILE = "singbox.txt"

# Iran Standard Time is a fixed UTC+03:30 (Iran abolished DST in 2022), so a
# fixed offset is correct and avoids a tzdata dependency in the container.
_TEHRAN = timezone(timedelta(hours=3, minutes=30))


def _display_name(source_channel: str, posted_at: datetime | None,
                  first_seen: datetime | None) -> str:
    """Build ``V2Get🛰️ YYYY-MM-DD HH:MM`` (channel post time, Tehran).

    The source channel is intentionally omitted from the public config name;
    falls back to ``first_seen`` when the channel post time is unknown (rows
    collected before we tracked it, or non-channel sources like npvt).
    """
    when = posted_at or first_seen
    stamp = ""
    if when is not None:
        if when.tzinfo is None:  # stored datetimes are naive UTC
            when = when.replace(tzinfo=timezone.utc)
        stamp = when.astimezone(_TEHRAN).strftime("%Y-%m-%d %H:%M")
    return f"V2Get🛰️ {stamp}".rstrip()


async def _raw_lines(session: AsyncSession, active_only: bool) -> list[str]:
    stmt = select(
        Config.raw, Config.source_channel, Config.posted_at, Config.first_seen
    )
    if active_only:
        stmt = stmt.where(Config.active.is_(True))
    stmt = stmt.order_by(Config.last_seen.desc())
    rows = (await session.execute(stmt)).all()
    return [
        rename(raw, _display_name(source_channel, posted_at, first_seen))
        for raw, source_channel, posted_at, first_seen in rows
        if raw
    ]


async def generate(session: AsyncSession) -> dict[str, str]:
    """Write all enabled output files. Returns ``{filename: content}``."""
    config.ensure_dirs()
    active = await _raw_lines(session, active_only=True)
    archive = await _raw_lines(session, active_only=False)

    active_text = "\n".join(active) + ("\n" if active else "")
    base64_text = base64.b64encode(active_text.encode("utf-8")).decode("ascii")
    archive_text = "\n".join(archive) + ("\n" if archive else "")

    files: dict[str, str] = {
        ACTIVE_FILE: active_text,
        BASE64_FILE: base64_text,
        ARCHIVE_FILE: archive_text,
    }

    if settings.get("output_clash"):
        files[CLASH_FILE] = clash_export.clash_yaml(active)
    if settings.get("output_stash"):
        files[STASH_FILE] = clash_export.stash_yaml(active)
    if settings.get("output_singbox"):
        files[SINGBOX_FILE] = _singbox(active)

    for name, content in files.items():
        (config.output_dir / name).write_text(content, encoding="utf-8")

    return files


def published_files() -> list[str]:
    """Files intended for GitHub publishing (excludes the bulky archive)."""
    names = [ACTIVE_FILE, BASE64_FILE]
    if settings.get("output_clash"):
        names.append(CLASH_FILE)
    if settings.get("output_stash"):
        names.append(STASH_FILE)
    if settings.get("output_singbox"):
        names.append(SINGBOX_FILE)
    return names


def read_output(name: str) -> str | None:
    p = config.output_dir / name
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8", errors="ignore")


def _singbox(links: list[str]) -> str:
    return json.dumps({"outbounds": [{"type": "raw", "uri": ln} for ln in links]}, indent=2)
