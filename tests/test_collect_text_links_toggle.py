"""The ``collect_text_links`` switch: .npvt-only subscription mode.

With the switch off the collector must stop harvesting links from channel
*message text* while everything else in a run still happens - the active pool
is re-validated, rotated, written out and published - so a subscription built
purely from the isolated .npvt pipeline stays fresh.

These tests assert the contract rather than a snapshot: no Telegram history is
read at all when the switch is off (the strongest possible guarantee that no
link can leak in from a channel message), and the .npvt injection path is
completely unaffected by the switch.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core import collector
from app.core.settings_manager import DEFAULTS, settings
from app.database import SessionLocal, init_db
from app.models import Channel, Config, RunLog, utcnow


@pytest.fixture(autouse=True)
def _restore_setting():
    """Never let one test's toggle state bleed into the next."""
    before = settings.get("collect_text_links")
    yield
    settings._cache["collect_text_links"] = before


def test_switch_exists_and_defaults_to_on():
    """Existing installs must keep harvesting text links until told otherwise."""
    assert "collect_text_links" in DEFAULTS
    assert DEFAULTS["collect_text_links"] is True


def test_accessor_coerces_to_bool():
    settings._cache["collect_text_links"] = 0
    assert settings.collect_text_links is False
    settings._cache["collect_text_links"] = 1
    assert settings.collect_text_links is True


class _SpyClient:
    """Stands in for the Telegram client and records whether it was asked."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch_new(self, username, last_id, limit):
        # The collector wraps this call in a broad ``except Exception`` so a
        # flaky channel can't kill a run; raising here would therefore be
        # swallowed. Record the call instead and assert on ``calls``.
        self.calls.append(username)
        return []


async def _seed_channels(names=("alpha", "beta")) -> None:
    async with SessionLocal() as session:
        existing = {
            c.username for c in
            (await session.execute(select(Channel))).scalars().all()
        }
        for name in names:
            if name not in existing:
                session.add(Channel(username=name, enabled=True, scan_limit=5))
        await session.commit()


async def _run_with_switch(monkeypatch, *, enabled: bool) -> tuple[dict, _SpyClient]:
    await init_db()
    await settings.load()
    await _seed_channels()

    spy = _SpyClient()
    monkeypatch.setattr(collector, "collector_client", spy)
    settings._cache["collect_text_links"] = enabled

    async with SessionLocal() as session:
        run = RunLog(started_at=utcnow())
        session.add(run)
        await session.flush()
        summary = await collector._execute(session, run)
        await session.commit()
    return summary, spy


def test_channel_history_is_never_read_when_switch_is_off(monkeypatch):
    """The decisive test: the client is not called at all, so no text link can enter."""
    summary, spy = asyncio.run(_run_with_switch(monkeypatch, enabled=False))
    assert spy.calls == []
    assert summary["messages_read"] == 0
    assert summary["configs_found"] == 0
    assert summary["channels_scanned"] == 0


def test_run_still_completes_and_publishes_when_switch_is_off(monkeypatch):
    """Pool maintenance and output generation must keep running."""
    summary, _ = asyncio.run(_run_with_switch(monkeypatch, enabled=False))
    assert summary["ok"] is True
    # The run reports a pool figure and a publish decision, i.e. it went all the
    # way through rotation -> outputs -> github, rather than short-circuiting.
    assert "active_pool" in summary
    assert "github_push" in summary


def test_switch_on_still_reads_channels(monkeypatch):
    """Guard against the switch silently disabling collection for everyone."""
    summary, spy = asyncio.run(_run_with_switch(monkeypatch, enabled=True))
    assert spy.calls, "collector skipped channels even though the switch was on"
    assert summary["channels_scanned"] == len(spy.calls)


def test_npvt_ingest_is_unaffected_by_the_switch():
    """.npvt links must flow into the pool even with text collection off."""
    from app.npvt import ingest, models as npvt_models
    from app.npvt.unlocker import unlock_to_links_sync

    async def scenario() -> dict:
        await init_db()
        await npvt_models.init_models()  # npvt keeps its own declarative base
        await settings.load()
        settings._cache["collect_text_links"] = False
        data = (Path(__file__).parent / "fixtures" / "locked_sample.npvt").read_bytes()
        links = unlock_to_links_sync(data).links
        assert links
        return await ingest.inject(links, source="npvt:switch_test")

    summary = asyncio.run(scenario())
    assert summary["parsed"] == summary["received"]
    assert summary["new"] + summary["refreshed"] == summary["parsed"]

    async def count_rows() -> int:
        async with SessionLocal() as session:
            rows = (await session.execute(
                select(Config).where(Config.source_channel == "npvt:switch_test")
            )).scalars().all()
            return len(rows)

    assert asyncio.run(count_rows()) > 0
