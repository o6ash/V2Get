"""Discovery must never enqueue a file id its transaction hasn't committed.

Regression test for a race that stranded files permanently:

    session.add(row); await session.flush()
    self._queue.put_nowait(row.id)      # <- a worker can pick this up NOW
    ...
    await session.commit()              # <- but the row only appears here

An unlock worker reads the row in its *own* session, cannot see the still-open
transaction, concludes the file "no longer exists" and drops it. The row then
does land in the table, so the next discovery pass treats it as already-known
and never re-enqueues it — the file sits in ``pending`` forever.

Observed live: file 2433 (@farsiproxy.npvt) was discovered and dropped within
the same second: "queue cleanup — file 2433 no longer exists, skipping".

Testing this needs care. Asserting visibility *after* ``_discover_once``
returns proves nothing: the commit has happened by then and the buggy ordering
passes too. So the check runs **at the moment of enqueue**, through a separate
plain ``sqlite3`` connection — precisely what another session sees. Under WAL a
second connection reads the last committed state, so an uncommitted row is
simply absent.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest
from sqlalchemy import select

from app.config import config
from app.database import SessionLocal, init_db
from app.npvt import models as npvt_models
from app.npvt import worker as worker_module
from app.npvt.models import PENDING, NpvtFile
from app.npvt.worker import NpvtWorker


class _Candidate:
    """Mimics source.NpvtCandidate without needing Telethon."""

    def __init__(self, channel: str, message_id: int, name: str) -> None:
        self.channel = channel
        self.message_id = message_id
        self.file_name = name
        self.file_size = 1234
        self.message = None


def _visible_to_another_connection(file_id: int) -> bool:
    """True if a separate DB connection can see the row (i.e. it is committed)."""
    con = sqlite3.connect(str(config.db_path), timeout=2.0)
    try:
        row = con.execute(
            "select 1 from npvt_files where id = ?", (file_id,)
        ).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        # A lock contention here means the writer's transaction is still open,
        # which is exactly the condition this test forbids.
        return False
    finally:
        con.close()


class _SpyQueue(asyncio.Queue):
    """Records, at enqueue time, whether each id was already committed."""

    def __init__(self) -> None:
        super().__init__()
        self.enqueued: list[int] = []
        self.visible_at_enqueue: list[bool] = []

    def put_nowait(self, item):  # type: ignore[override]
        self.enqueued.append(item)
        self.visible_at_enqueue.append(_visible_to_another_connection(item))
        super().put_nowait(item)


async def _discover_with(monkeypatch, candidates) -> NpvtWorker:
    await init_db()
    await npvt_models.init_models()

    async def fake_discover():
        return candidates

    monkeypatch.setattr(worker_module.source, "discover", fake_discover)

    w = NpvtWorker()
    w._queue = _SpyQueue()
    await w._discover_once()
    return w


def test_ids_are_committed_before_they_reach_the_queue(monkeypatch):
    """The decisive assertion — catches the enqueue-before-commit ordering."""
    cands = [
        _Candidate("chan_a", 9001, "one.npvt"),
        _Candidate("chan_a", 9002, "two.npvt"),
        _Candidate("chan_b", 9003, "three.npvt"),
    ]

    w = asyncio.run(_discover_with(monkeypatch, cands))
    assert w._queue.enqueued, "discovery enqueued nothing"
    assert all(w._queue.visible_at_enqueue), (
        "an id reached the queue before its row was committed — a worker "
        "reading it in another session would drop the file permanently"
    )


def test_queued_rows_are_pending_and_readable_by_a_worker_session(monkeypatch):
    """What the worker actually does: fetch the row in its own AsyncSession."""
    cands = [_Candidate("chan_e", 6001, "readable.npvt")]

    async def scenario() -> list[str]:
        w = await _discover_with(monkeypatch, cands)
        statuses = []
        async with SessionLocal() as session:
            for file_id in w._queue.enqueued:
                row = await session.get(NpvtFile, file_id)
                assert row is not None, f"worker cannot read queued id {file_id}"
                statuses.append(row.status)
        return statuses

    assert asyncio.run(scenario()) == [PENDING]


def test_discovery_is_idempotent_per_message(monkeypatch):
    """A second pass over the same messages must not enqueue duplicates.

    This is what makes the race unrecoverable: once the row exists, discovery
    will never offer that message again.
    """
    cands = [_Candidate("chan_c", 7001, "dup.npvt")]

    async def scenario() -> tuple[list[int], list[int], int]:
        w1 = await _discover_with(monkeypatch, cands)
        w2 = await _discover_with(monkeypatch, cands)
        async with SessionLocal() as session:
            rows = (await session.execute(
                select(NpvtFile).where(
                    NpvtFile.source_channel == "chan_c",
                    NpvtFile.source_message_id == 7001,
                )
            )).scalars().all()
        return w1._queue.enqueued, w2._queue.enqueued, len(rows)

    first, second, row_count = asyncio.run(scenario())
    assert len(first) == 1
    assert second == [], "the same message was enqueued twice"
    assert row_count == 1, "the same message created two rows"


def test_files_seen_counter_matches_queued_ids(monkeypatch):
    """The dashboard counter must not drift from what actually got queued."""
    cands = [_Candidate("chan_d", 8000 + i, f"f{i}.npvt") for i in range(4)]
    w = asyncio.run(_discover_with(monkeypatch, cands))
    assert w.stats["files_seen"] == len(w._queue.enqueued) == 4


def test_nothing_is_queued_when_there_are_no_candidates(monkeypatch):
    w = asyncio.run(_discover_with(monkeypatch, []))
    assert w._queue.enqueued == []
