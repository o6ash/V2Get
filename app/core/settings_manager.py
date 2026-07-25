"""Dashboard-editable settings, persisted in the DB and cached in memory.

Defaults seed the store on first boot. Updates take effect immediately for any
code path that reads through :data:`settings` — no container restart required.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from app.config import config as env_config
from app.database import SessionLocal
from app.models import Setting

DEFAULTS: dict[str, Any] = {
    # When False the collector stops harvesting proxy links from channel
    # *message text*. Everything else in a run still happens: the active pool
    # is re-validated over TCP, rotated, written out and published. The
    # isolated .npvt pipeline (app.npvt) is a separate path and is NOT
    # affected — turn this off to build subscriptions purely from .npvt files.
    "collect_text_links": True,
    "scan_interval_minutes": 15,
    "tcp_timeout_seconds": 3.0,
    "max_pool_size": 50,
    "cooldown_hours": 24,
    "fail_threshold": 3,
    "tcp_concurrency": 100,
    "github_repository": env_config.github_repository,
    "github_token": env_config.github_token,
    "github_branch": env_config.github_branch or "main",
    "github_target_dir": env_config.github_target_dir,
    # output formats to generate (always includes active + base64)
    "output_clash": False,
    "output_stash": False,
    "output_singbox": False,
    "stat_retention_points": 2000,
}


class SettingsManager:
    def __init__(self) -> None:
        self._cache: dict[str, Any] = dict(DEFAULTS)

    async def load(self) -> None:
        """Load persisted values over the defaults, seeding any that are absent."""
        async with SessionLocal() as session:
            rows = (await session.execute(select(Setting))).scalars().all()
            stored = {r.key: r.value for r in rows}
            for key, default in DEFAULTS.items():
                if key in stored:
                    self._cache[key] = _decode(stored[key], default)
                else:
                    session.add(Setting(key=key, value=_encode(default)))
            await session.commit()

    def get(self, key: str, default: Any = None) -> Any:
        return self._cache.get(key, DEFAULTS.get(key, default))

    def all(self) -> dict[str, Any]:
        return dict(self._cache)

    def public(self) -> dict[str, Any]:
        """Settings safe to expose to the UI (token masked)."""
        data = dict(self._cache)
        token = data.get("github_token") or ""
        data["github_token"] = f"{'*' * 8}{token[-4:]}" if token else ""
        data["github_token_set"] = bool(token)
        return data

    async def update(self, values: dict[str, Any]) -> dict[str, Any]:
        async with SessionLocal() as session:
            for key, raw in values.items():
                if key not in DEFAULTS:
                    continue
                # Ignore masked token resubmissions (the UI shows "****abcd").
                if key == "github_token" and isinstance(raw, str) and "*" in raw:
                    continue
                value = _coerce(key, raw)
                self._cache[key] = value
                existing = await session.get(Setting, key)
                if existing:
                    existing.value = _encode(value)
                else:
                    session.add(Setting(key=key, value=_encode(value)))
            await session.commit()
        return self.all()

    # typed convenience accessors -------------------------------------------------
    @property
    def collect_text_links(self) -> bool:
        return bool(self.get("collect_text_links"))

    @property
    def scan_interval_minutes(self) -> int:
        return int(self.get("scan_interval_minutes"))

    @property
    def tcp_timeout(self) -> float:
        return float(self.get("tcp_timeout_seconds"))

    @property
    def max_pool_size(self) -> int:
        return int(self.get("max_pool_size"))

    @property
    def cooldown_hours(self) -> int:
        return int(self.get("cooldown_hours"))

    @property
    def fail_threshold(self) -> int:
        return int(self.get("fail_threshold"))

    @property
    def tcp_concurrency(self) -> int:
        return int(self.get("tcp_concurrency"))


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


settings = SettingsManager()
