"""Async SQLAlchemy engine / session management."""
from __future__ import annotations

from collections.abc import AsyncGenerator

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import config


class Base(DeclarativeBase):
    pass


# ``timeout`` becomes SQLite's busy_timeout: writers wait for the lock instead
# of failing fast with "database is locked". Needed now that the background
# npvt worker writes concurrently with the collector's run transaction.
_engine = create_async_engine(
    config.db_url, echo=False, future=True, connect_args={"timeout": 30},
)
SessionLocal = async_sessionmaker(_engine, expire_on_commit=False, class_=AsyncSession)


@event.listens_for(_engine.sync_engine, "connect")
def _sqlite_pragmas(dbapi_connection, _record) -> None:
    """Apply per-connection SQLite pragmas, most importantly WAL.

    WAL (Write-Ahead Logging) lets readers run concurrently with a writer, so
    the dashboard stays responsive while the collector run holds its write
    transaction (in the default rollback-journal mode a writer blocks readers).
    ``synchronous=NORMAL`` is the safe/fast companion to WAL; ``busy_timeout``
    makes writers wait for the lock instead of erroring.
    """
    cur = dbapi_connection.cursor()
    try:
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.execute("PRAGMA foreign_keys=ON")
    finally:
        cur.close()


# Additive, idempotent column migrations — SQLite's create_all() never ALTERs an
# existing table, so new columns on shipped models are added here on startup.
_COLUMN_MIGRATIONS: dict[str, dict[str, str]] = {
    "channels": {
        "scan_limit": "INTEGER NOT NULL DEFAULT 15",
    },
    "configs": {
        "posted_at": "DATETIME",
    },
}


def _apply_migrations(sync_conn) -> None:
    insp = inspect(sync_conn)
    tables = set(insp.get_table_names())
    for table, columns in _COLUMN_MIGRATIONS.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for name, ddl in columns.items():
            if name not in existing:
                sync_conn.exec_driver_sql(
                    f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"
                )


async def init_db() -> None:
    config.ensure_dirs()
    # Import models so they register with the metadata before create_all.
    from app import models  # noqa: F401

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_apply_migrations)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
