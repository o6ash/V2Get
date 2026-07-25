"""ORM models private to the npvt module.

These live on their own :class:`NpvtBase` declarative base so the module never
has to be registered in :mod:`app.models`. They are created with the shared
engine on startup (see :func:`app.npvt.models.init_models`). SQLAlchemy sessions
are base-agnostic, so the same :data:`app.database.SessionLocal` is reused.
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


class NpvtBase(DeclarativeBase):
    """Private metadata so npvt tables never collide with the core models."""


# Lifecycle states for a discovered .npvt file.
PENDING = "pending"
PROCESSING = "processing"
DONE = "done"
FAILED = "failed"
SKIPPED = "skipped"  # duplicate content or feature disabled mid-flight


class NpvtFile(NpvtBase):
    """One discovered .npvt attachment and the outcome of processing it.

    Deduplicated twice: by ``(source_channel, source_message_id)`` so the same
    message is never enqueued twice, and by ``file_hash`` so identical content
    reposted in another message/channel is skipped.
    """

    __tablename__ = "npvt_files"
    __table_args__ = (UniqueConstraint("source_channel", "source_message_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_channel: Mapped[str] = mapped_column(String(255), index=True)
    source_message_id: Mapped[int] = mapped_column(Integer, default=0)
    file_name: Mapped[str] = mapped_column(String(255), default="")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    file_hash: Mapped[str] = mapped_column(String(64), default="", index=True)

    status: Mapped[str] = mapped_column(String(16), default=PENDING, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    links_found: Mapped[int] = mapped_column(Integer, default=0)
    links_injected: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class NpvtLink(NpvtBase):
    """A raw V2Ray link collected from the bot, for module-level dedup/audit.

    Fingerprint-level dedup against the global archive happens in the core
    pipeline; this table prevents re-injecting the *exact same raw link* that a
    previous file already produced, and gives the dashboard a provenance trail.
    """

    __tablename__ = "npvt_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    link_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    raw: Mapped[str] = mapped_column(Text)
    protocol: Mapped[str] = mapped_column(String(16), default="")
    file_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    injected: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class NpvtChannelCursor(NpvtBase):
    """Per-channel high-water mark so discovery never rescans old messages."""

    __tablename__ = "npvt_channel_cursors"

    username: Mapped[str] = mapped_column(String(255), primary_key=True)
    last_message_id: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class NpvtSetting(NpvtBase):
    """Key/value store for the npvt module's own dashboard-editable settings."""

    __tablename__ = "npvt_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


async def init_models() -> None:
    """Create the npvt tables on the shared engine (idempotent)."""
    from app.database import _engine

    async with _engine.begin() as conn:
        await conn.run_sync(NpvtBase.metadata.create_all)
