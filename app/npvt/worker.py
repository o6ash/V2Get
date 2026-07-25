"""Background worker: discovery -> dedup -> bot relay -> pipeline injection.

Architecture (all asyncio, event-driven):

  * one *discovery* loop that periodically scans channels and enqueues new files
  * N *relay* workers that drain the queue concurrently (``relay_concurrency``)
  * a shared rate-limiter enforcing a minimum gap between bot sends
  * per-job retry with backoff, and a hard per-relay timeout

Every stage is wrapped so a failure (bot down, no buttons, timeout, parse error)
is recorded against the file and never propagates — the core collector is wholly
unaffected. All three feature toggles are re-read live each cycle, so the
dashboard can pause any stage without a restart.
"""
from __future__ import annotations

import asyncio
import hashlib
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.core.logbook import get_logger
from app.database import SessionLocal
from app.models import Channel
from app.npvt import ingest, source
from app.npvt.bot_relay import CaptchaRequired, RelayError, RelayUnavailable, relay
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

log = get_logger()


class _RateLimiter:
    """Enforce a minimum (jittered) interval between successive bot sends.

    Jitter makes the send cadence look less robotic, which helps avoid tripping
    the bot's anti-flood CAPTCHA in the first place.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last = 0.0

    async def wait(self, min_interval: float, jitter: float = 0.0) -> None:
        async with self._lock:
            loop = asyncio.get_event_loop()
            gap = max(0.0, min_interval - (loop.time() - self._last))
            if jitter > 0:
                gap += random.uniform(0.0, jitter)
            if gap > 0:
                await asyncio.sleep(gap)
            self._last = asyncio.get_event_loop().time()


class _CircuitBreaker:
    """Pauses relay after a CAPTCHA, with escalating cooldown.

    We never solve CAPTCHAs; we detect them and back off. Repeated trips lengthen
    the cooldown (base · 2ⁿ, capped) and the worker auto-disables relay after a
    configurable number of consecutive trips. A clean relay resets the streak.
    """

    def __init__(self) -> None:
        self.resume_at: datetime | None = None
        self.reason = ""
        self.consecutive = 0
        self.trips_total = 0

    def is_open(self) -> bool:
        return self.resume_at is not None and datetime.now(timezone.utc) < self.resume_at

    def remaining_seconds(self) -> float:
        if not self.is_open():
            return 0.0
        return (self.resume_at - datetime.now(timezone.utc)).total_seconds()

    def trip(self, base: float, cap: float, reason: str) -> float:
        self.consecutive += 1
        self.trips_total += 1
        cooldown = min(cap, base * (2 ** (self.consecutive - 1))) if base > 0 else 0.0
        self.resume_at = datetime.now(timezone.utc) + timedelta(seconds=cooldown)
        self.reason = reason
        return cooldown

    def reset(self) -> None:
        self.resume_at = None
        self.reason = ""
        self.consecutive = 0

    def state(self) -> dict:
        return {
            "paused": self.is_open(),
            "reason": self.reason,
            "resume_at": self.resume_at.isoformat() if self.resume_at else None,
            "remaining_seconds": int(self.remaining_seconds()),
            "consecutive_trips": self.consecutive,
            "trips_total": self.trips_total,
        }


class NpvtWorker:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[int] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._rate = _RateLimiter()
        self._breaker = _CircuitBreaker()
        self._running = False
        self._scan_wake = asyncio.Event()
        self._inflight: set[int] = set()
        # lightweight live counters surfaced to the dashboard
        self.stats = {
            "files_seen": 0, "files_done": 0, "files_failed": 0,
            "files_skipped": 0, "files_filtered": 0, "links_collected": 0,
            "links_injected": 0, "captchas_seen": 0, "last_activity": None,
        }

    def breaker_state(self) -> dict:
        return self._breaker.state()

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        await self._requeue_unfinished()
        workers = npvt_settings.relay_concurrency
        self._tasks = [
            asyncio.create_task(self._discovery_loop(), name="npvt-discovery"),
            *[asyncio.create_task(self._relay_worker(i), name=f"npvt-relay-{i}")
              for i in range(workers)],
        ]
        log.info("npvt worker started (%d relay worker(s))", workers)

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
                self._queue.put_nowait(row.id)
                self.stats["files_seen"] += 1
            await session.commit()

    # ── relay workers ──────────────────────────────────────────────────────---
    async def _relay_worker(self, idx: int) -> None:
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
        # If relay is paused, defer this file without holding a DB session.
        if not npvt_settings.relay_enabled:
            asyncio.create_task(self._requeue_after(
                file_id, min(30.0, float(npvt_settings.scan_interval_seconds))))
            return

        # CAPTCHA back-off in effect: defer until the circuit breaker closes.
        if self._breaker.is_open():
            delay = self._breaker.remaining_seconds() + random.uniform(1.0, 5.0)
            asyncio.create_task(self._requeue_after(file_id, delay))
            return

        # ── Queue cleanup before relay ─────────────────────────────────────────
        # The queue is storage, not a forced execution list: re-validate the file
        # right before sending so duplicates / already-processed / disallowed
        # files never reach the bot.
        decision = await self._screen_before_relay(file_id)
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
            await self._fail(file_id, "download failed or exceeded size cap",
                             retry=attempt <= npvt_settings.max_retries)
            return
        digest = hashlib.sha256(data).hexdigest()
        if await self._is_duplicate_content(file_id, digest):
            await self._finish(file_id, SKIPPED, file_hash=digest,
                               note="duplicate content already processed")
            self.stats["files_skipped"] += 1
            return

        # Bot relay -------------------------------------------------------------
        await self._rate.wait(
            npvt_settings.relay_min_interval_seconds, npvt_settings.relay_jitter_seconds)
        try:
            result = await relay.relay(data, file_name)
        except CaptchaRequired as exc:
            # Don't burn the retry budget — this isn't the file's fault. Back off
            # and re-queue it for after the cooldown.
            await self._on_captcha(file_id, str(exc))
            return
        except RelayUnavailable as exc:
            await self._fail(file_id, f"relay unavailable: {exc}", retry=False,
                             file_hash=digest)
            return
        except RelayError as exc:
            retry = attempt <= npvt_settings.max_retries
            await self._fail(file_id, str(exc), retry=retry, file_hash=digest)
            return

        links = result.links
        self.stats["links_collected"] += len(links)
        log.info("npvt: %s -> %d link(s) via %r [%s], %d batch(es)%s",
                 file_name, len(links), result.button_clicked, result.strategy,
                 result.batches, " [captcha after]" if result.captcha else "")

        # Pipeline injection (keep whatever links we got, even if a captcha
        # appeared afterwards) -------------------------------------------------
        injected = 0
        if links and npvt_settings.link_collection_enabled:
            summary = await ingest.inject(
                links, source=f"npvt:{channel}", file_id=file_id)
            injected = summary["new"] + summary["refreshed"]
            self.stats["links_injected"] += injected

        await self._finish(file_id, DONE, file_hash=digest,
                           links_found=len(links), links_injected=injected)

        if result.captcha:
            # Links collected, but the bot then challenged us — back off so the
            # next files don't pile straight into the CAPTCHA wall.
            self._trip_breaker("captcha after link collection")
            await self._maybe_auto_disable()
        else:
            self._breaker.reset()  # clean relay — the bot is happy again
        self.stats["files_done"] += 1
        self.stats["last_activity"] = datetime.now(timezone.utc).isoformat()

    # ── queue cleanup (decide whether a queued file may reach the bot) ────────
    async def _screen_before_relay(self, file_id: int) -> str:
        """Return 'ok' to relay, 'drop' to skip permanently, 'defer' to retry later.

        Filters out files that should never be forwarded to the bot even though
        they sit in the queue: already-processed/stale entries, duplicate
        content, or files whose source channel was removed or disabled.
        """
        async with SessionLocal() as session:
            row = await session.get(NpvtFile, file_id)
            if row is None:
                log.info("npvt: queue cleanup — file %d no longer exists, not relaying", file_id)
                return "drop"

            if row.status in (DONE, SKIPPED, FAILED):
                log.info("npvt: queue cleanup — file %d already terminal (%s), not relaying",
                         file_id, row.status)
                return "drop"

            # Known-duplicate content (hash from a prior download) already handled
            # by another file — don't re-send the same payload to the bot.
            if row.file_hash and await self._is_duplicate_content(file_id, row.file_hash):
                log.info("npvt: queue cleanup — file %d duplicate content, not relaying", file_id)
                await self._finish(file_id, SKIPPED, file_hash=row.file_hash,
                                   note="duplicate content (pre-relay cleanup)")
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

    # ── captcha back-off ───────────────────────────────────────────────────--
    def _trip_breaker(self, reason: str) -> float:
        cooldown = self._breaker.trip(
            npvt_settings.captcha_cooldown_seconds,
            npvt_settings.captcha_cooldown_max_seconds,
            reason,
        )
        self.stats["captchas_seen"] += 1
        log.warning(
            "npvt: CAPTCHA detected (%s) — relay backing off for %.0fs "
            "(consecutive=%d). We do not solve CAPTCHAs.",
            reason, cooldown, self._breaker.consecutive,
        )
        return cooldown

    async def _maybe_auto_disable(self) -> None:
        if (npvt_settings.captcha_auto_disable_relay
                and self._breaker.consecutive >= npvt_settings.captcha_max_consecutive):
            await npvt_settings.update({"relay_enabled": False})
            log.error(
                "npvt: %d consecutive CAPTCHAs — Bot relay AUTO-DISABLED. Let the "
                "bot cool down, then re-enable it on the NPVT page.",
                self._breaker.consecutive,
            )

    async def _on_captcha(self, file_id: int, reason: str) -> None:
        """Handle a CAPTCHA seen *before* any links: back off and re-queue."""
        cooldown = self._trip_breaker(reason)
        async with SessionLocal() as session:
            row = await session.get(NpvtFile, file_id)
            if row is not None:
                row.status = PENDING
                row.error = "deferred: CAPTCHA back-off"
                # This wasn't the file's fault — don't count it against retries.
                row.attempts = max(0, row.attempts - 1)
                await session.commit()
        await self._maybe_auto_disable()
        # Re-queue for after the cooldown (only if relay wasn't auto-disabled;
        # if it was, the relay-disabled guard will keep deferring anyway).
        asyncio.create_task(
            self._requeue_after(file_id, cooldown + random.uniform(1.0, 5.0)))

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

        Drains the in-memory queue so relay workers stop pulling cancelled IDs,
        then deletes every ``pending``/``processing`` row. Finished files
        (done/failed/skipped) are kept. Any stale ID left in a relay worker's
        hand or in a pending ``_requeue_after`` task is harmless: the relay
        re-fetches the row by id and no-ops when it has been deleted (see the
        ``row is None`` guards). Files still present in their channels will be
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
