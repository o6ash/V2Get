"""Background worker for the .ovpn pipeline.

One event-driven loop:

  discover -> download -> parse -> dedup -> TCP health check -> store locally
           -> periodic re-check of known files -> publish to GitHub

Everything is wrapped so no failure can escape into the core collector, and
both toggles (``collection_enabled`` / ``publish_enabled``) are re-read every
cycle so the dashboard can pause a stage without a restart.

Database writes are deliberately short and batched: SQLite serialises writers,
so long transactions here would contend with a running collection.
"""
from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta

from sqlalchemy import select

from app.core.logbook import get_logger
from app.core.tcp_checker import tcp_alive
from app.database import SessionLocal
from app.ovpn import parser, publisher, source
from app.ovpn.config import ovpn_settings
from app.ovpn.models import DONE, FAILED, SKIPPED, OvpnFile, utcnow

log = get_logger()
_INDEX_HASH_KEY = "index_hash"
_LINKS_HASH_KEY = "links_hash"


class OvpnWorker:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._running = False
        self._wake = asyncio.Event()
        self.stats = {
            "files_seen": 0, "files_done": 0, "files_failed": 0,
            "files_skipped": 0, "healthy": 0, "published": 0,
            "last_activity": None, "last_publish": None,
        }

    # ── lifecycle ────────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="ovpn-worker")
        log.info("ovpn worker started")

    async def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("ovpn worker stopped")

    def trigger_scan(self) -> None:
        self._wake.set()

    # ── main loop ────────────────────────────────────────────────────────────
    async def _loop(self) -> None:
        while self._running:
            try:
                if ovpn_settings.collection_enabled:
                    await self._collect_once()
                    await self._recheck_health()
                if ovpn_settings.publish_enabled:
                    await self._publish_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the loop must never die
                log.exception("ovpn: cycle error: %s", exc)
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=ovpn_settings.scan_interval_seconds
                )
            except asyncio.TimeoutError:
                pass
            finally:
                self._wake.clear()

    # ── collection ───────────────────────────────────────────────────────────
    async def _collect_once(self) -> None:
        candidates = await source.discover()
        if not candidates:
            return
        for c in candidates:
            if not self._running or not ovpn_settings.collection_enabled:
                return
            try:
                await self._process(c)
            except Exception as exc:  # noqa: BLE001 - one bad file never stops the rest
                log.warning("ovpn: processing failed for %s/%s: %s",
                            c.channel, c.message_id, exc)
            # Yield between files so the collector and dashboard stay responsive.
            await asyncio.sleep(0)

    async def _process(self, c) -> None:
        async with SessionLocal() as session:
            exists = (await session.execute(
                select(OvpnFile.id).where(
                    OvpnFile.source_channel == c.channel,
                    OvpnFile.source_message_id == c.message_id,
                )
            )).scalar_one_or_none()
        if exists:
            return

        self.stats["files_seen"] += 1
        data = await source.download_by_id(
            c.channel, c.message_id, ovpn_settings.max_file_bytes
        )
        row = OvpnFile(
            source_channel=c.channel, source_message_id=c.message_id,
            file_name=c.file_name, file_size=c.file_size,
        )
        if data is None:
            row.status = FAILED
            row.error = "download failed or size cap exceeded"
            self.stats["files_failed"] += 1
            await self._save(row)
            return

        try:
            profile = parser.parse(data)
        except parser.ParseError as exc:
            row.status = SKIPPED
            row.error = str(exc)
            self.stats["files_skipped"] += 1
            await self._save(row)
            return

        row.file_hash = profile.content_hash
        row.file_size = row.file_size or len(data)
        row.remote_host = profile.host
        row.remote_port = profile.port
        row.remote_proto = profile.proto

        async with SessionLocal() as session:
            dup = (await session.execute(
                # A hash can legitimately match several rows (the same profile
                # reposted many times), so ask for existence, not uniqueness.
                select(OvpnFile.id)
                .where(OvpnFile.file_hash == profile.content_hash)
                .limit(1)
            )).scalars().first()
        if dup:
            row.status = SKIPPED
            row.error = "duplicate content"
            self.stats["files_skipped"] += 1
            await self._save(row)
            return

        row.healthy = await self._health(profile.host, profile.port, profile.proto)
        row.checked_at = utcnow()
        row.stored_name = parser.stored_name(c.channel, c.message_id, c.file_name)
        try:
            parser.write_local(row.stored_name, profile.text)
        except OSError as exc:
            row.status = FAILED
            row.error = f"local write failed: {exc}"
            self.stats["files_failed"] += 1
            await self._save(row)
            return

        row.status = DONE
        self.stats["files_done"] += 1
        self.stats["last_activity"] = utcnow().isoformat()
        await self._save(row)

    async def _health(self, host: str, port: int, proto: str) -> bool:
        if not ovpn_settings.health_check_enabled:
            return True
        if proto == "udp":
            # A UDP endpoint cannot be validated with a TCP connect; treat it as
            # usable when the operator opted in, otherwise drop it.
            return ovpn_settings.include_udp
        return await tcp_alive(host, port, timeout=ovpn_settings.tcp_timeout)

    async def _save(self, row: OvpnFile) -> None:
        async with SessionLocal() as session:
            session.add(row)
            await session.commit()

    # ── periodic health re-check ─────────────────────────────────────────────
    async def _recheck_health(self) -> None:
        cutoff = utcnow() - timedelta(minutes=ovpn_settings.recheck_interval_minutes)
        cutoff = cutoff.replace(tzinfo=None)
        async with SessionLocal() as session:
            rows = (await session.execute(
                select(OvpnFile).where(
                    OvpnFile.status == DONE,
                    OvpnFile.remote_host != "",
                ).order_by(OvpnFile.checked_at.asc()).limit(200)
            )).scalars().all()
            stale = [r for r in rows if r.checked_at is None or r.checked_at <= cutoff]
            for r in stale:
                r.healthy = await self._health(r.remote_host, r.remote_port, r.remote_proto)
                r.checked_at = utcnow()
            if stale:
                await session.commit()
                log.info("ovpn: re-checked %d file(s)", len(stale))

    # ── publishing ───────────────────────────────────────────────────────────
    async def _publish_once(self) -> None:
        async with SessionLocal() as session:
            rows = (await session.execute(
                select(OvpnFile).where(
                    OvpnFile.status == DONE,
                    OvpnFile.healthy.is_(True),
                    OvpnFile.stored_name != "",
                ).order_by(OvpnFile.id.desc()).limit(ovpn_settings.max_index_entries)
            )).scalars().all()
            selected = list(rows)

        self.stats["healthy"] = len(selected)
        if not selected:
            return

        # ── phase 1: upload blobs that are not on the remote yet ──────────────
        # Each cycle is capped by max_files_per_push, so the index must never be
        # written in the same breath as the files it references — otherwise a
        # subscriber fetching index.txt gets 404s for everything not yet pushed.
        pending = [r for r in selected if not r.published][:ovpn_settings.max_files_per_push]
        files: dict[str, str] = {}
        for r in pending:
            text = parser.read_local(r.stored_name)
            if text:
                # Published as .txt so the raw link renders instead of downloading.
                files[publisher.published_name(r.stored_name)] = text
        if files:
            result = await publisher.publish(files)
            if result.get("status") == "failed":
                log.warning("ovpn: publish failed: %s", result.get("reason"))
            pushed = set(result.get("files") or [])
            done_ids = {
                r.id for r in pending
                if publisher.published_name(r.stored_name) in pushed
            }
            if done_ids:
                async with SessionLocal() as session:
                    marked = (await session.execute(
                        select(OvpnFile).where(OvpnFile.id.in_(done_ids))
                    )).scalars().all()
                    for r in marked:
                        r.published = True
                    await session.commit()
                # Keep the in-memory view in sync so the index below can include
                # the rows that just went live, without a second query.
                for r in selected:
                    if r.id in done_ids:
                        r.published = True
                self.stats["published"] += len(done_ids)
                self.stats["last_publish"] = utcnow().isoformat()

        # ── phase 2: the subscription itself — profile *contents*, inlined ────
        # Read from local storage, so this never depends on what is already on
        # the remote: the bundle is self-contained and usable the moment it is
        # published, with no per-file links to resolve.
        entries: list[tuple[str, str]] = []
        for r in selected:
            text = parser.read_local(r.stored_name)
            if text:
                entries.append((r.stored_name, text))
        if entries:
            index_text, included = publisher.build_index(
                entries, ovpn_settings.max_index_bytes
            )
            index_hash = hashlib.sha256(index_text.encode("utf-8")).hexdigest()
            if index_hash != await ovpn_settings.get_state(_INDEX_HASH_KEY):
                result = await publisher.publish({ovpn_settings.index_file: index_text})
                if result.get("status") == "success":
                    await ovpn_settings.set_state(_INDEX_HASH_KEY, index_hash)
                    self.stats["last_publish"] = utcnow().isoformat()
                    log.info(
                        "ovpn: subscription published — %d profile(s), %d bytes",
                        len(included), len(index_text.encode("utf-8")),
                    )
                elif result.get("status") == "failed":
                    log.warning("ovpn: subscription push failed: %s", result.get("reason"))

        # ── phase 3: companion link list — only blobs live on the remote ──────
        live = [r for r in selected if r.published]
        if not live:
            return
        links_text = publisher.build_links([r.stored_name for r in live])
        links_hash = hashlib.sha256(links_text.encode("utf-8")).hexdigest()
        if links_hash == await ovpn_settings.get_state(_LINKS_HASH_KEY):
            return
        result = await publisher.publish({ovpn_settings.links_file: links_text})
        if result.get("status") != "success":
            if result.get("status") == "failed":
                log.warning("ovpn: links push failed: %s", result.get("reason"))
            return
        await ovpn_settings.set_state(_LINKS_HASH_KEY, links_hash)
        log.info("ovpn: links published with %d entr(ies)", len(live))


worker = OvpnWorker()
