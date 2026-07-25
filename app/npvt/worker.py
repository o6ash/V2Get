"""Background worker: discovery -> dedup -> local unlock -> pipeline injection.

Architecture (all asyncio, event-driven):

  * one *discovery* loop that periodically scans channels and enqueues new files
  * N *unlock* workers that drain the queue concurrently (``unlock_concurrency``)
  * per-job retry with backoff for transient failures (downloads)

Unlocking happens **in-process** (see :mod:`app.npvt.unlocker`) — no external
bot, no button-clicking, no CAPTCHA and no send pacing. The cipher is CPU-bound
pure Python, so it is dispatched to a worker thread and never blocks the event
loop shared with the dashboard and the core collector.

Every stage is wrapped so a failure (download error, corrupt container, parse
error) is recorded against the file and never propagates — the core collector is
wholly unaffected. Both feature toggles are re-read live each cycle, so the
dashboard can pause a stage without a restart.
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timezone

from sqlalchemy import delete, select

from app.core.logbook import get_logger
from app.database import SessionLocal
from app.models import Channel
from app.npvt import ingest, source
from app.npvt.config import npvt_settings
from app.npvt.models import (
    DONE,
    FAILED,
    PENDING,
    PROCESSING,
    SKIPPED,
    NpvtFile,
    utcnow,
)
from app.npvt.unlocker import UnlockError, unlock_to_links

log = get_logger()


class NpvtWorker:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._scan_wake = asyncio.Event()
        self._inflight: set[int] = set()
        # lightweight live counters surfaced to the dashboard
        self.stats = {
            "files_seen": 0, "files_done": 0, "files_failed": 0,
            "files_skipped": 0, "files_filtered": 0, "links_collected": 0,
            "links_injected": 0, "last_activity": None,
        }

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self._requeue_unfinished()
        workers = npvt_settings.unlock_concurrency
        self._tasks = [
            asyncio.create_task(self._discovery_loop(), name="npvt-discovery"),
            *[asyncio.create_task(self._unlock_worker(i), name=f"npvt-unlock-{i}")
              for i in range(workers)],
        ]
        log.info("npvt worker started (%d unlock worker(s))", workers)

    async def stop(self) -> None:
        self._running = False
        self._scan_wake.set()
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except asyncio.CancelledError:
                pass
        self._tasks = []
        log.info("npvt worker stopped")

    def trigger_scan(self) -> None:
        self._scan_wake.set()

    @property
    def queue_size(self) -> int:
        return self._queue.qsize() + len(self._inflight)

    async def _requeue_unfinished(self) -> None:
        """Re-enqueue files left pending/processing by a previous run."""
        async with SessionLocal() as session:
            rows = (await session.execute(
                select(NpvtFile).where(NpvtFile.status.in_([PENDING, PROCESSING]))
            )).scalars().all()
            for row in rows:
                row.status = PENDING
                # Restart-safe: the worker re-fetches the document by
                # (channel, message_id), so re-queuing is enough to resume.
                self._queue.put_nowait(row.id)
            if rows:
                await session.commit()
                log.info("npvt: re-queued %d unfinished file(s)", len(rows))

    # ── discovery ──────────────────────────────────────────────────────────---
    async def _discovery_loop(self) -> None:
        while self._running:
            try:
                if npvt_settings.collection_enabled:
                    await self._discover_once()
            except Exception as exc:  # noqa: BLE001 - discovery must never die
                log.exception("npvt: discovery loop error: %s", exc)
            interval = npvt_settings.scan_interval_seconds
            try:
                await asyncio.wait_for(self._scan_wake.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
            finally:
                self._scan_wake.clear()

    async def _discover_once(self) -> None:
        candidates = await source.discover()
        if not candidates:
            return
        # Ids are collected here and only enqueued AFTER the commit below.
        # Enqueuing a flushed-but-uncommitted id is a race: an unlock worker
        # reads the row in its own session, cannot see the open transaction,
        # concludes the file "no longer exists" and drops it permanently — and
        # because the row does land in the table a moment later, the next
        # discovery pass treats it as already-known and never re-enqueues it.
        # The file would be stranded in `pending` forever.
        new_ids: list[int] = []
        async with SessionLocal() as session:
            for c in candidates:
                exists = (await session.execute(
                    select(NpvtFile).where(
                        NpvtFile.source_channel == c.channel,
                        NpvtFile.source_message_id == c.message_id,
                    )
                )).scalar_one_or_none()
                if exists:
                    continue
                row = NpvtFile(
                    source_channel=c.channel, source_message_id=c.message_id,
                    file_name=c.file_name, file_size=c.file_size, status=PENDING,
                )
                session.add(row)
                await session.flush()
                new_ids.append(row.id)
                self.stats["files_seen"] += 1
            await session.commit()

        for file_id in new_ids:
            self._queue.put_nowait(file_id)

    # ── unlock workers ─────────────────────────────────────────────────────---
    async def _unlock_worker(self, idx: int) -> None:
        while self._running:
            try:
                file_id = await self._queue.get()
            except asyncio.CancelledError:
                break
            self._inflight.add(file_id)
            try:
                await self._process_file(file_id)
            except Exception as exc:  # noqa: BLE001 - never let a worker die
                log.exception("npvt: worker %d crashed on file %d: %s", idx, file_id, exc)
            finally:
                self._inflight.discard(file_id)
                self._queue.task_done()

    async def _process_file(self, file_id: int) -> None:
        # ── Queue cleanup before unlocking ────────────────────────────────────
        # The queue is storage, not a forced execution list: re-validate the file
        # right before working on it so duplicates / already-processed / files
        # from removed channels are never downloaded again.
        decision = await self._screen_before_unlock(file_id)
        if decision == "drop":
            self.stats["files_filtered"] += 1
            return
        if decision == "defer":
            asyncio.create_task(self._requeue_after(
                file_id, min(60.0, float(npvt_settings.scan_interval_seconds))))
            return

        async with SessionLocal() as session:
            row = await session.get(NpvtFile, file_id)
            if row is None:
                return
            row.status = PROCESSING
            row.attempts += 1
            await session.commit()
            attempt = row.attempts
            channel = row.source_channel
            message_id = row.source_message_id
            file_name = row.file_name

        # Download + content-hash dedup ----------------------------------------
        data = await source.download_by_id(
            channel, message_id, npvt_settings.max_file_bytes)
        if not data:
            # Transient (network / Telegram hiccup): worth another attempt.
            await self._fail(file_id, "download failed or exceeded size cap",
                             retry=attempt <= npvt_settings.max_retries)
            return
        digest = hashlib.sha256(data).hexdigest()
        if await self._is_duplicate_content(file_id, digest):
            await self._finish(file_id, SKIPPED, file_hash=digest,
                               note="duplicate content already processed")
            self.stats["files_skipped"] += 1
            return

        # Local unlock ----------------------------------------------------------
        try:
            result = await unlock_to_links(data, file_name)
        except UnlockError as exc:
            # Deterministic: the same bytes will fail identically every time, so
            # burning retries on it only delays the queue. Fail permanently.
            await self._fail(file_id, f"unlock failed: {exc}", retry=False,
                             file_hash=digest)
            return

        links = result.links
        self.stats["links_collected"] += len(links)
        log.info("npvt: %s -> %d link(s) from %d profile(s) [%s]",
                 file_name, len(links), result.profiles,
                 "was locked" if result.was_locked else "already unlocked")

        # Pipeline injection ----------------------------------------------------
        injected = 0
        if links and npvt_settings.link_collection_enabled:
            summary = await ingest.inject(
                links, source=f"npvt:{channel}", file_id=file_id)
            injected = summary["new"] + summary["refreshed"]
            self.stats["links_injected"] += injected

        await self._finish(file_id, DONE, file_hash=digest,
                           links_found=len(links), links_injected=injected)
        self.stats["files_done"] += 1
        self.stats["last_activity"] = datetime.now(timezone.utc).isoformat()

    # ── queue cleanup (decide whether a queued file may be processed) ─────────
    async def _screen_before_unlock(self, file_id: int) -> str:
        """Return 'ok' to process, 'drop' to skip permanently, 'defer' to retry later.

        Filters out files that should never be downloaded again even though they
        sit in the queue: already-processed/stale entries, duplicate content, or
        files whose source channel was removed or disabled.
        """
        async with SessionLocal() as session:
            row = await session.get(NpvtFile, file_id)
            if row is None:
                log.info("npvt: queue cleanup — file %d no longer exists, skipping", file_id)
                return "drop"

            if row.status in (DONE, SKIPPED, FAILED):
                log.info("npvt: queue cleanup — file %d already terminal (%s), skipping",
                         file_id, row.status)
                return "drop"

            # Known-duplicate content (hash from a prior download) already handled
            # by another file — don't re-download the same payload.
            if row.file_hash and await self._is_duplicate_content(file_id, row.file_hash):
                log.info("npvt: queue cleanup — file %d duplicate content, skipping", file_id)
                await self._finish(file_id, SKIPPED, file_hash=row.file_hash,
                                   note="duplicate content (pre-unlock cleanup)")
                return "drop"

            # Source channel must still exist and be enabled.
            channel = (await session.execute(
                select(Channel).where(Channel.username == row.source_channel)
            )).scalar_one_or_none()
            if channel is None:
                log.info("npvt: queue cleanup — file %d source channel %s removed, skipping",
                         file_id, row.source_channel)
                await self._finish(file_id, SKIPPED, note="source channel removed")
                return "drop"
            if not channel.enabled:
                log.info("npvt: queue cleanup — file %d source channel %s disabled, deferring",
                         file_id, row.source_channel)
                return "defer"

        return "ok"

    # ── persistence helpers ────────────────────────────────────────────────---
    async def _is_duplicate_content(self, file_id: int, digest: str) -> bool:
        async with SessionLocal() as session:
            other = (await session.execute(
                select(NpvtFile).where(
                    NpvtFile.file_hash == digest,
                    NpvtFile.id != file_id,
                    NpvtFile.status.in_([DONE, SKIPPED]),
                ).limit(1)
            )).scalar_one_or_none()
            return other is not None

    async def _finish(self, file_id: int, status: str, *, file_hash: str = "",
                      links_found: int = 0, links_injected: int = 0,
                      note: str = "") -> None:
        async with SessionLocal() as session:
            row = await session.get(NpvtFile, file_id)
            if row is None:
                return
            row.status = status
            if file_hash:
                row.file_hash = file_hash
            row.links_found = links_found
            row.links_injected = links_injected
            row.error = note
            row.updated_at = utcnow()
            await session.commit()

    async def _fail(self, file_id: int, reason: str, *, retry: bool,
                    file_hash: str = "") -> None:
        async with SessionLocal() as session:
            row = await session.get(NpvtFile, file_id)
            if row is None:
                return
            if file_hash:
                row.file_hash = file_hash
            if retry and self._running:
                row.status = PENDING
                row.error = f"retry pending: {reason}"
                attempts = row.attempts
                await session.commit()
                backoff = npvt_settings.retry_backoff_seconds * max(1, attempts)
                log.warning("npvt: file %d failed (%s) — retrying in %.0fs (attempt %d)",
                            file_id, reason, backoff, attempts)
                asyncio.create_task(self._requeue_after(file_id, backoff))
            else:
                row.status = FAILED
                row.error = reason
                row.updated_at = utcnow()
                await session.commit()
                self.stats["files_failed"] += 1
                log.error("npvt: file %d permanently failed: %s", file_id, reason)

    async def _requeue_after(self, file_id: int, delay: float) -> None:
        try:
            await asyncio.sleep(max(0.0, delay))
        except asyncio.CancelledError:
            return
        if self._running:
            self._queue.put_nowait(file_id)

    async def clear_queue(self) -> int:
        """Cancel and delete all pending/processing files; drain the queue.

        Drains the in-memory queue so unlock workers stop pulling cancelled IDs,
        then deletes every ``pending``/``processing`` row. Finished files
        (done/failed/skipped) are kept. Any stale ID left in a worker's hand or
        in a pending ``_requeue_after`` task is harmless: the worker re-fetches
        the row by id and no-ops when it has been deleted (see the ``row is
        None`` guards). Files still present in their channels will be
        re-discovered on the next scan. Returns the number of rows deleted.
        """
        drained = 0
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            drained += 1

        async with SessionLocal() as session:
            result = await session.execute(
                delete(NpvtFile).where(NpvtFile.status.in_([PENDING, PROCESSING]))
            )
            await session.commit()
            deleted = int(result.rowcount or 0)
        log.info(
            "npvt: queue cleared — drained %d queued id(s), deleted %d file(s)",
            drained, deleted,
        )
        return deleted

    async def retry_file(self, file_id: int) -> bool:
        """Manually re-enqueue a file (e.g. after a permanent failure)."""
        async with SessionLocal() as session:
            row = await session.get(NpvtFile, file_id)
            if row is None:
                return False
            row.status = PENDING
            row.error = "manual retry"
            await session.commit()
        self._queue.put_nowait(file_id)
        return True


worker = NpvtWorker()
