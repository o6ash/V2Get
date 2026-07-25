"""Dashboard-editable settings private to the npvt module.

Mirrors the design of :mod:`app.core.settings_manager` but is backed by the
module's own :class:`~app.npvt.models.NpvtSetting` table, so the core settings
store is never modified. Defaults seed the table on first load; updates apply
immediately for every code path that reads through :data:`npvt_settings`.

Unlocking is local (:mod:`app.npvt.unlocker`), so there is no bot username, no
button matching, no send pacing and no CAPTCHA back-off to configure — those
knobs were removed along with the relay. Keys left behind in the table by an
older build are pruned on load.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import delete, select

from app.database import SessionLocal
from app.npvt.models import NpvtSetting

# Default settings. The two feature toggles come first.
DEFAULTS: dict[str, Any] = {
    # ── master feature toggles ────────────────────────────────────────────────
    "collection_enabled": False,        # detect & download .npvt files from channels
    "link_collection_enabled": False,   # inject the unlocked links into the pipeline
    # ── discovery ─────────────────────────────────────────────────────────────
    "scan_interval_seconds": 300,
    "scan_message_limit": 200,
    "max_file_bytes": 5_000_000,
    # ── unlock / robustness ───────────────────────────────────────────────────
    # Unlocking is CPU-bound pure Python dispatched to worker threads; a couple
    # of workers keeps the queue moving without starving the event loop.
    "unlock_concurrency": 2,
    "max_retries": 3,
    "retry_backoff_seconds": 10.0,
    # ── ingest ────────────────────────────────────────────────────────────────
    "publish_after_ingest": True,       # regenerate outputs / push to GitHub after inject
}


class NpvtSettingsManager:
    def __init__(self) -> None:
        self._cache: dict[str, Any] = dict(DEFAULTS)

    async def load(self) -> None:
        async with SessionLocal() as session:
            rows = (await session.execute(select(NpvtSetting))).scalars().all()
            stored = {r.key: r.value for r in rows}
            for key, default in DEFAULTS.items():
                if key in stored:
                    self._cache[key] = _decode(stored[key], default)
                else:
                    session.add(NpvtSetting(key=key, value=_encode(default)))
            # Drop rows for settings this build no longer has (the bot-relay and
            # CAPTCHA knobs). Harmless if left, but they would keep showing up in
            # the settings API and confuse the dashboard form.
            obsolete = [k for k in stored if k not in DEFAULTS]
            if obsolete:
                await session.execute(
                    delete(NpvtSetting).where(NpvtSetting.key.in_(obsolete))
                )
            await session.commit()

    def get(self, key: str, default: Any = None) -> Any:
        return self._cache.get(key, DEFAULTS.get(key, default))

    def all(self) -> dict[str, Any]:
        return dict(self._cache)

    async def update(self, values: dict[str, Any]) -> dict[str, Any]:
        async with SessionLocal() as session:
            for key, raw in values.items():
                if key not in DEFAULTS:
                    continue
                value = _coerce(key, raw)
                self._cache[key] = value
                existing = await session.get(NpvtSetting, key)
                if existing:
                    existing.value = _encode(value)
                else:
                    session.add(NpvtSetting(key=key, value=_encode(value)))
            await session.commit()
        return self.all()

    # typed convenience accessors -------------------------------------------------
    @property
    def collection_enabled(self) -> bool:
        return bool(self.get("collection_enabled"))

    @property
    def link_collection_enabled(self) -> bool:
        return bool(self.get("link_collection_enabled"))

    @property
    def scan_interval_seconds(self) -> int:
        return max(10, int(self.get("scan_interval_seconds")))

    @property
    def scan_message_limit(self) -> int:
        return max(1, int(self.get("scan_message_limit")))

    @property
    def max_file_bytes(self) -> int:
        return max(0, int(self.get("max_file_bytes")))

    @property
    def unlock_concurrency(self) -> int:
        return max(1, int(self.get("unlock_concurrency")))

    @property
    def max_retries(self) -> int:
        return max(0, int(self.get("max_retries")))

    @property
    def retry_backoff_seconds(self) -> float:
        return max(0.0, float(self.get("retry_backoff_seconds")))

    @property
    def publish_after_ingest(self) -> bool:
        return bool(self.get("publish_after_ingest"))


def _encode(value: Any) -> str:
    return json.dumps(value)


def _decode(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _coerce(key: str, raw: Any) -> Any:
    default = DEFAULTS[key]
    if isinstance(default, bool):
        if isinstance(raw, str):
            return raw.lower() in ("1", "true", "on", "yes")
        return bool(raw)
    if isinstance(default, int):
        return int(float(raw))
    if isinstance(default, float):
        return float(raw)
    return str(raw)


npvt_settings = NpvtSettingsManager()
