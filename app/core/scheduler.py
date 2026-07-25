"""Non-overlapping periodic scheduler.

Runs once immediately at startup, then every ``scan_interval_minutes``. The
interval is read fresh each cycle so dashboard changes apply without a restart.
A manual trigger event lets the dashboard force an immediate run, and the
collector's own lock guarantees runs never overlap.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from app.core import collector
from app.core.logbook import get_logger
from app.core.settings_manager import settings

log = get_logger()


class Scheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stop = False
        self.last_run: datetime | None = None
        self.next_run: datetime | None = None
        self.last_summary: dict | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop = False
            self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self) -> None:
        self._stop = True
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def trigger_now(self) -> None:
        """Request an immediate run (used by the dashboard button)."""
        self._wake.set()

    @property
    def running(self) -> bool:
        return collector.is_running()

    def state(self) -> dict:
        return {
            "status": "running" if self.running else "idle",
            "last_run": self.last_run.isoformat() if self.last_run else None,
            "next_run": self.next_run.isoformat() if self.next_run else None,
            "interval_minutes": settings.scan_interval_minutes,
            "last_summary": self.last_summary,
        }

    async def _run_and_record(self, trigger: str) -> None:
        self.last_run = datetime.now(timezone.utc)
        try:
            self.last_summary = await collector.run_once(trigger=trigger)
        except Exception as exc:  # noqa: BLE001
            log.exception("Scheduler run error: %s", exc)
        interval = max(1, settings.scan_interval_minutes)
        self.next_run = datetime.now(timezone.utc) + timedelta(minutes=interval)

    async def _loop(self) -> None:
        log.info("Scheduler started — first run immediately")
        await self._run_and_record("startup")

        while not self._stop:
            interval = max(1, settings.scan_interval_minutes)
            timeout = (self.next_run - datetime.now(timezone.utc)).total_seconds() \
                if self.next_run else interval * 60
            timeout = max(1.0, min(timeout, interval * 60))
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
                manual = True
            except asyncio.TimeoutError:
                manual = False
            finally:
                self._wake.clear()

            if self._stop:
                break
            await self._run_and_record("manual" if manual else "scheduled")


scheduler = Scheduler()
