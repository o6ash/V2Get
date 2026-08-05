"""Deterministic eviction order + archive backfill.

Covers the contract: the active pool holds the newest healthy configs, dead
members are evicted first, then the oldest ones (even when healthy), and free
slots are refilled from the archive only with TCP-passing rows.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete

from app.core import pool_manager
from app.core.settings_manager import settings
from app.database import SessionLocal, init_db
from app.models import Config, Cooldown

BASE = datetime(2026, 1, 1)


@pytest.fixture(autouse=True)
async def _fresh_db():
    await init_db()
    async with SessionLocal() as session:
        await session.execute(delete(Config))
        await session.execute(delete(Cooldown))
        await session.commit()
    yield


def _config(n: int, *, active: bool = False, alive: bool = True, age_min: int = 0) -> Config:
    return Config(
        fingerprint=f"vless:fp{n}", protocol="vless", raw=f"vless://u{n}@h{n}.com:443",
        name=f"node{n}", host=f"h{n}.com", port=443, active=active, alive=alive,
        posted_at=BASE + timedelta(minutes=age_min),
        first_seen=BASE + timedelta(minutes=age_min),
    )


async def _active_fps(session) -> set[str]:
    return {r.fingerprint for r in await pool_manager.active_configs(session)}


# ── health gate ──────────────────────────────────────────────────────────────

async def test_dead_candidates_never_enter_the_pool():
    settings._cache["max_pool_size"] = 5
    async with SessionLocal() as session:
        good = _config(1, alive=True)
        bad = _config(2, alive=False)
        session.add_all([good, bad])
        await session.flush()

        added, removed = await pool_manager.add_to_pool(session, [good, bad])
        await session.commit()

        assert (added, removed) == (1, 0)
        assert await _active_fps(session) == {"vless:fp1"}


# ── eviction order ───────────────────────────────────────────────────────────

async def test_dead_members_are_evicted_before_healthy_ones():
    settings._cache["max_pool_size"] = 3
    async with SessionLocal() as session:
        # Pool is full: one TCP-failed member, two healthy ones.
        session.add_all([
            _config(1, active=True, alive=False, age_min=100),  # dead -> first out
            _config(2, active=True, alive=True, age_min=10),
            _config(3, active=True, alive=True, age_min=20),
        ])
        fresh = [_config(50, age_min=999)]
        session.add_all(fresh)
        await session.flush()

        added, removed = await pool_manager.add_to_pool(session, fresh)
        await session.commit()

        assert (added, removed) == (1, 1)
        assert await _active_fps(session) == {"vless:fp2", "vless:fp3", "vless:fp50"}


async def test_oldest_healthy_member_is_evicted_when_no_dead_ones_remain():
    settings._cache["max_pool_size"] = 3
    async with SessionLocal() as session:
        session.add_all([
            _config(1, active=True, age_min=10),   # oldest -> evicted
            _config(2, active=True, age_min=20),
            _config(3, active=True, age_min=30),
        ])
        fresh = [_config(50, age_min=999)]
        session.add_all(fresh)
        await session.flush()

        added, removed = await pool_manager.add_to_pool(session, fresh)
        await session.commit()

        assert (added, removed) == (1, 1)
        assert await _active_fps(session) == {"vless:fp2", "vless:fp3", "vless:fp50"}


async def test_trim_drops_dead_and_oldest_first():
    settings._cache["max_pool_size"] = 10
    async with SessionLocal() as session:
        session.add_all([
            _config(1, active=True, alive=False, age_min=900),
            _config(2, active=True, age_min=10),
            _config(3, active=True, age_min=20),
            _config(4, active=True, age_min=30),
        ])
        await session.flush()

        settings._cache["max_pool_size"] = 2
        trimmed = await pool_manager.trim_to_size(session)
        await session.commit()

        assert trimmed == 2
        assert await _active_fps(session) == {"vless:fp3", "vless:fp4"}


# ── backfill ─────────────────────────────────────────────────────────────────

async def test_backfill_refills_free_slots_with_newest_passing_configs(monkeypatch):
    settings._cache["max_pool_size"] = 3

    async def fake_check(rows, *, key, timeout, concurrency):
        # h2.com is unreachable; everything else answers.
        return [key(r)[0] != "h2.com" for r in rows]

    monkeypatch.setattr(pool_manager, "check_iter", fake_check)

    async with SessionLocal() as session:
        session.add_all([
            _config(1, active=True, age_min=5),
            _config(2, age_min=40),   # newest archived but TCP fails
            _config(3, age_min=30),
            _config(4, age_min=20),
            _config(5, age_min=10),
        ])
        await session.flush()

        promoted, checked = await pool_manager.backfill(session)
        await session.commit()

        assert promoted == 2                       # 3 slots - 1 already active
        assert checked >= 3
        assert await _active_fps(session) == {"vless:fp1", "vless:fp3", "vless:fp4"}
        assert await pool_manager.active_count(session) == 3


async def test_backfill_is_a_noop_when_the_pool_is_full():
    settings._cache["max_pool_size"] = 2
    async with SessionLocal() as session:
        session.add_all([_config(1, active=True), _config(2, active=True), _config(3)])
        await session.flush()

        assert await pool_manager.backfill(session) == (0, 0)


async def test_backfill_skips_configs_in_cooldown(monkeypatch):
    settings._cache["max_pool_size"] = 2
    settings._cache["fail_threshold"] = 1
    settings._cache["cooldown_hours"] = 24

    async def fake_check(rows, *, key, timeout, concurrency):
        return [True] * len(rows)

    monkeypatch.setattr(pool_manager, "check_iter", fake_check)

    from app.core import cooldown_manager

    async with SessionLocal() as session:
        session.add_all([_config(1, age_min=40), _config(2, age_min=30)])
        await session.flush()
        await cooldown_manager.record_failure(session, "vless:fp1")
        await session.commit()

        promoted, _checked = await pool_manager.backfill(session)
        await session.commit()

        assert promoted == 1
        assert await _active_fps(session) == {"vless:fp2"}
