"""ORM models private to the ovpn module.

Same isolation strategy as :mod:`app.npvt.models`: a private declarative base
so these tables never touch the core metadata, created with the shared engine
on startup. Nothing in :mod:`app.core` or :mod:`app.models` is modified.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OvpnBase(DeclarativeBase):
    """Private metadata so ovpn tables never collide with the core models."""


# Lifecycle states for a discovered .ovpn file.
PENDING = "pending"
PROCESSING = "processing"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"  # duplicate content, unparsable, or feature disabled


class OvpnFile(OvpnBase):
    """One discovered .ovpn attachment plus its parsed/health metadata.

    Deduplicated by ``(source_channel, source_message_id)`` so a message is
    never processed twice, and by ``file_hash`` so identical content reposted
    elsewhere is skipped.
    """

    __tablename__ = "ovpn_files"
    __table_args__ = (UniqueConstraint("source_channel", "source_message_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_channel: Mapped[str] = mapped_column(String(255), index=True)
    source_message_id: Mapped[int] = mapped_column(Integer, default=0)
    file_name: Mapped[str] = mapped_column(String(255), default="")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    file_hash: Mapped[str] = mapped_column(String(64), default="", index=True)

    # Parsed from the config body.
    remote_host: Mapped[str] = mapped_column(String(255), default="")
    remote_port: Mapped[int] = mapped_column(Integer, default=0)
    remote_proto: Mapped[str] = mapped_column(String(8), default="")

    # Health (TCP connect; UDP remotes are not probed).
    healthy: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    status: Mapped[str] = mapped_column(String(16), default=PENDING, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")

    # Local file name under ``<output_dir>/ovpn`` and remote publish state.
    stored_name: Mapped[str] = mapped_column(String(255), default="", index=True)
    published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class OvpnChannelCursor(OvpnBase):
    """Per-channel high-water mark so discovery never rescans old messages.

    Deliberately separate from ``npvt_channel_cursors``: the two modules scan
    for different extensions and must advance independently.
    """

    __tablename__ = "ovpn_channel_cursors"

    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    last_message_id: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class OvpnSetting(OvpnBase):
    """Key/value store for the ovpn module's own settings and push state."""

    __tablename__ = "ovpn_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


async def init_models() -> None:
    """Create the ovpn tables on the shared engine (idempotent)."""
    from app.database import _engine

    async with _engine.begin() as conn:
        await conn.run_sync(OvpnBase.metadata.create_all)
