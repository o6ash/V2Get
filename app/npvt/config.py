"""Dashboard-editable settings private to the npvt module.

Mirrors the design of :mod:`app.core.settings_manager` but is backed by the
module's own :class:`~app.npvt.models.NpvtSetting` table, so the core settings
store is never modified. Defaults seed the table on first load; updates apply
immediately for every code path that reads through :data:`npvt_settings`.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.npvt.models import NpvtSetting

# Default settings. The three feature toggles requested by the spec come first.
DEFAULTS: dict[str, Any] = {
    # ── master feature toggles ────────────────────────────────────────────────
    "collection_enabled": False,        # detect & download .npvt files from channels
    "relay_enabled": False,             # forward files to the bot and drive its buttons
    "link_collection_enabled": False,   # inject the bot's returned links into the pipeline
    # ── bot interaction ───────────────────────────────────────────────────────
    "bot_username": "DickiriptorBot",
    # Button chosen by TEXT match (case/diacritic-insensitive "contains"). The
    # spec's "second button / Get V2Ray link / لینک ویتوریشو بده" maps here.
    "button_text_patterns": [
        "get v2ray", "v2ray link", "get link", "v2ray",
        "لینک ویتوری", "ویتوری", "لینک v2ray", "دریافت لینک", "لینک",
    ],
    # Used only when no pattern matches: the spec says it's "usually the second
    # button" (0-based index 1). Text matching always takes precedence.
    "button_fallback_index": 1,
    # ── discovery ─────────────────────────────────────────────────────────────
    "scan_interval_seconds": 300,
    "scan_message_limit": 200,
    "max_file_bytes": 5_000_000,
    # ── relay timing / robustness ─────────────────────────────────────────────
    "relay_concurrency": 1,             # parallel bot conversations (1 = safest)
    "relay_min_interval_seconds": 5.0,  # rate-limit between successive bot sends
    "relay_timeout_seconds": 90.0,      # hard ceiling for one bot conversation
    "button_response_timeout_seconds": 45.0,
    "collect_window_seconds": 30.0,     # total time to gather links after the click
    "collect_quiet_seconds": 8.0,       # stop early after this long with no new links
    "max_retries": 3,
    "retry_backoff_seconds": 10.0,
    "relay_jitter_seconds": 3.0,        # random jitter added to send spacing
    # ── captcha back-off (we do NOT solve; we detect, pause and alert) ──────────
    "captcha_detection_enabled": True,
    # Phrase-like patterns (case/diacritic-insensitive "contains"). Kept specific
    # to avoid false positives; tune from the dashboard if the bot's wording differs.
    "captcha_text_patterns": [
        "what number is this", "what is the number", "enter the number",
        "enter the code", "type the number", "captcha",
        "چه عددی", "عدد داخل", "عدد را وارد", "کد امنیتی", "کد را وارد",
    ],
    "captcha_cooldown_seconds": 1800.0,        # base relay pause after a captcha (30m)
    "captcha_cooldown_max_seconds": 21600.0,   # cap for escalating pauses (6h)
    "captcha_max_consecutive": 3,              # consecutive trips before auto-disable
    "captcha_auto_disable_relay": True,        # turn relay off after repeated captchas
    # ── ingest ────────────────────────────────────────────────────────────────
    "publish_after_ingest": True,       # regenerate outputs / push to GitHub after inject
}

# Settings that must be exposed as JSON lists rather than scalars.
_LIST_KEYS = {"button_text_patterns", "captcha_text_patterns"}


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
    def relay_enabled(self) -> bool:
        return bool(self.get("relay_enabled"))

    @property
    def link_collection_enabled(self) -> bool:
        return bool(self.get("link_collection_enabled"))

    @property
    def bot_username(self) -> str:
        return str(self.get("bot_username") or "").strip().lstrip("@")

    @property
    def button_text_patterns(self) -> list[str]:
        pats = self.get("button_text_patterns") or []
        return [str(p) for p in pats if str(p).strip()]

    @property
    def button_fallback_index(self) -> int:
        return max(0, int(self.get("button_fallback_index")))

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
    def relay_concurrency(self) -> int:
        return max(1, int(self.get("relay_concurrency")))

    @property
    def relay_min_interval_seconds(self) -> float:
        return max(0.0, float(self.get("relay_min_interval_seconds")))

    @property
    def relay_timeout_seconds(self) -> float:
        return max(5.0, float(self.get("relay_timeout_seconds")))

    @property
    def button_response_timeout_seconds(self) -> float:
        return max(2.0, float(self.get("button_response_timeout_seconds")))

    @property
    def collect_window_seconds(self) -> float:
        return max(1.0, float(self.get("collect_window_seconds")))

    @property
    def collect_quiet_seconds(self) -> float:
        return max(0.5, float(self.get("collect_quiet_seconds")))

    @property
    def max_retries(self) -> int:
        return max(0, int(self.get("max_retries")))

    @property
    def retry_backoff_seconds(self) -> float:
        return max(0.0, float(self.get("retry_backoff_seconds")))

    @property
    def relay_jitter_seconds(self) -> float:
        return max(0.0, float(self.get("relay_jitter_seconds")))

    @property
    def captcha_detection_enabled(self) -> bool:
        return bool(self.get("captcha_detection_enabled"))

    @property
    def captcha_text_patterns(self) -> list[str]:
        pats = self.get("captcha_text_patterns") or []
        return [str(p) for p in pats if str(p).strip()]

    @property
    def captcha_cooldown_seconds(self) -> float:
        return max(0.0, float(self.get("captcha_cooldown_seconds")))

    @property
    def captcha_cooldown_max_seconds(self) -> float:
        return max(self.captcha_cooldown_seconds, float(self.get("captcha_cooldown_max_seconds")))

    @property
    def captcha_max_consecutive(self) -> int:
        return max(1, int(self.get("captcha_max_consecutive")))

    @property
    def captcha_auto_disable_relay(self) -> bool:
        return bool(self.get("captcha_auto_disable_relay"))

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
    if key in _LIST_KEYS:
        if isinstance(raw, str):
            items = [s.strip() for s in raw.replace("\n", ",").split(",")]
            return [s for s in items if s]
        if isinstance(raw, list):
            return [str(s).strip() for s in raw if str(s).strip()]
        return list(default)
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
