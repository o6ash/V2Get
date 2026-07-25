"""ORM models — the persistent state of the platform.

Everything that must survive a container restart is stored here (the DB file
lives on a mounted volume). Output/blacklist/log text files are derived
artifacts written under the same volume.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Setting(Base):
    """Key/value store for dashboard-editable settings."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_message_id: Mapped[int] = mapped_column(Integer, default=0)
    # How many recent messages to scan per run for this channel (per-channel,
    # overrides the old global limit). See app.database._COLUMN_MIGRATIONS.
    scan_limit: Mapped[int] = mapped_column(Integer, default=15)

    # Cumulative per-channel statistics.
    messages_scanned: Mapped[int] = mapped_column(Integer, default=0)
    configs_found: Mapped[int] = mapped_column(Integer, default=0)
    configs_accepted: Mapped[int] = mapped_column(Integer, default=0)
    duplicates_removed: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Config(Base):
    """Every unique config ever discovered (the archive).

    ``active`` marks membership of the rotating active pool.
    """

    __tablename__ = "configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fingerprint: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    protocol: Mapped[str] = mapped_column(String(16), index=True)
    raw: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(String(255), default="")
    host: Mapped[str] = mapped_column(String(255), default="")
    port: Mapped[int] = mapped_column(Integer, default=0)

    active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    alive: Mapped[bool] = mapped_column(Boolean, default=False)
    source_channel: Mapped[str] = mapped_column(String(255), default="")
    # When the config was posted in its source channel (UTC). Distinct from
    # first_seen (when v2get scanned it); null for pre-existing/non-channel rows.
    posted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_checked: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Cooldown(Base):
    __tablename__ = "cooldowns"

    fingerprint: Mapped[str] = mapped_column(String(80), primary_key=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RunLog(Base):
    """Structured summary of a single collector run."""

    __tablename__ = "run_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    channels_scanned: Mapped[int] = mapped_column(Integer, default=0)
    messages_read: Mapped[int] = mapped_column(Integer, default=0)
    configs_found: Mapped[int] = mapped_column(Integer, default=0)
    duplicates_removed: Mapped[int] = mapped_column(Integer, default=0)
    tcp_failed: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_skipped: Mapped[int] = mapped_column(Integer, default=0)
    added: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    active_pool: Mapped[int] = mapped_column(Integer, default=0)
    github_push: Mapped[str] = mapped_column(String(32), default="skipped")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    detail: Mapped[str] = mapped_column(Text, default="")


class StatPoint(Base):
    """Time-series snapshot for historical charts."""

    __tablename__ = "stat_points"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    active_count: Mapped[int] = mapped_column(Integer, default=0)
    archive_count: Mapped[int] = mapped_column(Integer, default=0)
    cooldown_count: Mapped[int] = mapped_column(Integer, default=0)
    alive_rate: Mapped[float] = mapped_column(Float, default=0.0)


class GithubState(Base):
    """Last-known GitHub publish state (single row, id=1)."""

    __tablename__ = "github_state"
    __table_args__ = (UniqueConstraint("id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_status: Mapped[str] = mapped_column(String(32), default="never")
    last_commit: Mapped[str] = mapped_column(String(64), default="")
    last_push_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_content_hash: Mapped[str] = mapped_column(String(64), default="")
