"""Dashboard-editable settings private to the ovpn module.

Backed by the module's own :class:`~app.ovpn.models.OvpnSetting` table, so the
core settings store is never written to. Internal (non-user) state such as the
last published content hash is kept in the same table under keys prefixed with
``_`` and is hidden from :meth:`OvpnSettingsManager.all`.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.database import SessionLocal
from app.ovpn.models import OvpnSetting

DEFAULTS: dict[str, Any] = {
    # ── master feature toggles ────────────────────────────────────────────────
    "collection_enabled": False,     # detect & download .ovpn files from channels
    "publish_enabled": False,        # push the ovpn/ folder + index to GitHub
    # ── discovery ─────────────────────────────────────────────────────────────
    "scan_interval_seconds": 900,
    "scan_message_limit": 200,
    "max_file_bytes": 500_000,
    # ── health ────────────────────────────────────────────────────────────────
    "health_check_enabled": True,
    "tcp_timeout": 4.0,
    "include_udp": True,             # UDP remotes cannot be TCP-probed; keep them
    "recheck_interval_minutes": 120,
    # ── publishing ────────────────────────────────────────────────────────────
    # Sub-directory *inside* the configured github_target_dir. Keeping the ovpn
    # payload under its own path is what makes the new subscription URL
    # independent of active.txt.
    "github_subdir": "ovpn",
    "index_file": "index.txt",
    "max_files_per_push": 20,        # cap commits per cycle
    "max_index_entries": 500,
}

# Internal state keys (never surfaced in the settings API).
_STATE_PREFIX = "_"


class OvpnSettingsManager:
    def __init__(self) -> None:
        self._cache: dict[str, Any] = dict(DEFAULTS)

    async def load(self) -> None:
        async with SessionLocal() as session:
            rows = (await session.execute(select(OvpnSetting))).scalars().all()
            stored = {r.key: r.value for r in rows}
            for key, default in DEFAULTS.items():
                if key in stored:
                    self._cache[key] = _decode(stored[key], default)
                else:
                    session.add(OvpnSetting(key=key, value=_encode(default)))
            await session.commit()

    def get(self, key: str, default: Any = None) -> Any:
        return self._cache.get(key, DEFAULTS.get(key, default))

    def all(self) -> dict[str, Any]:
        return {k: v for k, v in self._cache.items() if not k.startswith(_STATE_PREFIX)}

    async def update(self, values: dict[str, Any]) -> dict[str, Any]:
        async with SessionLocal() as session:
            for key, raw in values.items():
                if key not in DEFAULTS:
                    continue
                value = _coerce(key, raw)
                self._cache[key] = value
                existing = await session.get(OvpnSetting, key)
                if existing:
                    existing.value = _encode(value)
                else:
                    session.add(OvpnSetting(key=key, value=_encode(value)))
            await session.commit()
        return self.all()

    # ── internal state (push hash etc.) ───────────────────────────────────────
    async def get_state(self, key: str, default: str = "") -> str:
        async with SessionLocal() as session:
            row = await session.get(OvpnSetting, f"{_STATE_PREFIX}{key}")
            return row.value if row else default

    async def set_state(self, key: str, value: str) -> None:
        full = f"{_STATE_PREFIX}{key}"
        async with SessionLocal() as session:
            row = await session.get(OvpnSetting, full)
            if row:
                row.value = value
            else:
                session.add(OvpnSetting(key=full, value=value))
            await session.commit()

    # ── typed accessors ───────────────────────────────────────────────────────
    @property
    def collection_enabled(self) -> bool:
        return bool(self.get("collection_enabled"))

    @property
    def publish_enabled(self) -> bool:
        return bool(self.get("publish_enabled"))

    @property
    def scan_interval_seconds(self) -> int:
        return max(30, int(self.get("scan_interval_seconds")))

    @property
    def scan_message_limit(self) -> int:
        return max(1, int(self.get("scan_message_limit")))

    @property
    def max_file_bytes(self) -> int:
        return max(0, int(self.get("max_file_bytes")))

    @property
    def health_check_enabled(self) -> bool:
        return bool(self.get("health_check_enabled"))

    @property
    def tcp_timeout(self) -> float:
        return max(0.5, float(self.get("tcp_timeout")))

    @property
    def include_udp(self) -> bool:
        return bool(self.get("include_udp"))

    @property
    def recheck_interval_minutes(self) -> int:
        return max(5, int(self.get("recheck_interval_minutes")))

    @property
    def github_subdir(self) -> str:
        return (str(self.get("github_subdir")) or "ovpn").strip("/") or "ovpn"

    @property
    def index_file(self) -> str:
        return (str(self.get("index_file")) or "index.txt").strip("/") or "index.txt"

    @property
    def max_files_per_push(self) -> int:
        return max(1, int(self.get("max_files_per_push")))

    @property
    def max_index_entries(self) -> int:
        return max(1, int(self.get("max_index_entries")))


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


ovpn_settings = OvpnSettingsManager()
