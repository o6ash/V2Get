"""Database-backed behaviour: pool rotation and cooldown bookkeeping.

Runs against a real (throwaway) SQLite file created under the temporary
``DATA_DIR``, so the actual SQLAlchemy models and queries are exercised.
"""
from __future__ import annotations

import pytest
from sqlalchemy import delete

from app.core import cooldown_manager, pool_manager
from app.core.settings_manager import settings
from app.database import SessionLocal, init_db
from app.models import Config, Cooldown


@pytest.fixture(autouse=True)
async def _fresh_db():
    await init_db()
    async with SessionLocal() as session:
        await session.execute(delete(Config))
        await session.execute(delete(Cooldown))
        await session.commit()
    yield


def _config(n: int, *, active: bool = False, alive: bool = True) -> Config:
    return Config(
        fingerprint=f"vless:fp{n}", protocol="vless", raw=f"vless://u{n}@h{n}.com:443",
        name=f"node{n}", host=f"h{n}.com", port=443, active=active, alive=alive,
    )


# ── pool rotation ────────────────────────────────────────────────────────────

async def test_candidates_fill_an_empty_pool():
    settings._cache["max_pool_size"] = 5
    async with SessionLocal() as session:
        rows = [_config(i) for i in range(3)]
        for r in rows:
            session.add(r)
        await session.flush()

        added, removed = await pool_manager.add_to_pool(session, rows)
        await session.commit()

        assert (added, removed) == (3, 0)
        assert await pool_manager.active_count(session) == 3


async def test_pool_never_exceeds_max_size():
    settings._cache["max_pool_size"] = 4
    async with SessionLocal() as session:
        rows = [_config(i) for i in range(10)]
        for r in rows:
            session.add(r)
        await session.flush()
        await pool_manager.add_to_pool(session, rows)
        await session.commit()
        assert await pool_manager.active_count(session) == 4


async def test_full_pool_evicts_to_make_room_for_fresh_configs():
    settings._cache["max_pool_size"] = 3
    async with SessionLocal() as session:
        existing = [_config(i, active=True) for i in range(3)]
        for r in existing:
            session.add(r)
        fresh = [_config(100 + i) for i in range(2)]
        for r in fresh:
            session.add(r)
        await session.flush()

        added, removed = await pool_manager.add_to_pool(session, fresh)
        await session.commit()

        assert added == 2
        assert removed == 2                                   # one out per one in
        assert await pool_manager.active_count(session) == 3   # ceiling respected


async def test_lowering_the_ceiling_trims_the_pool():
    settings._cache["max_pool_size"] = 10
    async with SessionLocal() as session:
        for i in range(8):
            session.add(_config(i, active=True))
        await session.flush()

        settings._cache["max_pool_size"] = 3
        trimmed = await pool_manager.trim_to_size(session)
        await session.commit()

        assert trimmed == 5
        assert await pool_manager.active_count(session) == 3


async def test_trim_is_a_noop_when_already_under_the_ceiling():
    settings._cache["max_pool_size"] = 10
    async with SessionLocal() as session:
        session.add(_config(1, active=True))
        await session.flush()
        assert await pool_manager.trim_to_size(session) == 0


async def test_evict_removes_a_specific_config_from_the_pool():
    async with SessionLocal() as session:
        session.add(_config(1, active=True))
        await session.commit()

        assert await pool_manager.evict(session, "vless:fp1") is True
        await session.commit()
        assert await pool_manager.active_count(session) == 0
        # Evicting something already inactive is a safe no-op.
        assert await pool_manager.evict(session, "vless:fp1") is False


async def test_archive_counts_every_config_not_just_active_ones():
    async with SessionLocal() as session:
        session.add(_config(1, active=True))
        session.add(_config(2, active=False))
        await session.commit()
        assert await pool_manager.archive_count(session) == 2
        assert await pool_manager.active_count(session) == 1


# ── cooldown ─────────────────────────────────────────────────────────────────

async def test_failures_below_the_threshold_do_not_trip_cooldown():
    settings._cache["fail_threshold"] = 3
    async with SessionLocal() as session:
        assert await cooldown_manager.record_failure(session, "fp") is False
        assert await cooldown_manager.record_failure(session, "fp") is False
        assert await cooldown_manager.is_in_cooldown(session, "fp") is False
        await session.commit()


async def test_reaching_the_threshold_trips_cooldown():
    settings._cache["fail_threshold"] = 3
    settings._cache["cooldown_hours"] = 24
    async with SessionLocal() as session:
        for _ in range(2):
            await cooldown_manager.record_failure(session, "fp")
        assert await cooldown_manager.record_failure(session, "fp") is True
        assert await cooldown_manager.is_in_cooldown(session, "fp") is True
        await session.commit()


async def test_a_success_resets_the_failure_streak():
    settings._cache["fail_threshold"] = 3
    async with SessionLocal() as session:
        await cooldown_manager.record_failure(session, "fp")
        await cooldown_manager.record_failure(session, "fp")
        await cooldown_manager.record_success(session, "fp")
        # Streak reset: the next failure is the first, not the third.
        assert await cooldown_manager.record_failure(session, "fp") is False
        await session.commit()


async def test_success_clears_an_active_cooldown():
    settings._cache["fail_threshold"] = 1
    async with SessionLocal() as session:
        await cooldown_manager.record_failure(session, "fp")
        assert await cooldown_manager.is_in_cooldown(session, "fp") is True
        await cooldown_manager.record_success(session, "fp")
        assert await cooldown_manager.is_in_cooldown(session, "fp") is False
        await session.commit()


async def test_unknown_fingerprint_is_never_in_cooldown():
    async with SessionLocal() as session:
        assert await cooldown_manager.is_in_cooldown(session, "never-seen") is False


async def test_listing_and_counting_active_cooldowns():
    settings._cache["fail_threshold"] = 1
    async with SessionLocal() as session:
        await cooldown_manager.record_failure(session, "fp-a")
        await cooldown_manager.record_failure(session, "fp-b")
        await session.commit()

        assert await cooldown_manager.active_cooldown_count(session) == 2
        listed = await cooldown_manager.list_cooldowns(session, active_only=True)
        assert {row["fingerprint"] for row in listed} == {"fp-a", "fp-b"}
        assert all(row["remaining_seconds"] > 0 for row in listed)


async def test_manual_removal_clears_the_cooldown_entirely():
    settings._cache["fail_threshold"] = 1
    async with SessionLocal() as session:
        await cooldown_manager.record_failure(session, "fp")
        await session.commit()
        await cooldown_manager.remove_cooldown(session, "fp")
        await session.commit()
        assert await cooldown_manager.active_cooldown_count(session) == 0
