"""Process-level configuration sourced from the environment.

These are values that are intrinsic to *deployment* (secrets, paths) and are
read once at startup. User-tunable knobs (scan interval, pool size, timeouts …)
live in the database and are managed by :mod:`app.core.settings_manager` so they
can be changed from the dashboard without a restart.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Telegram
    telegram_api_id: int = 0

    @field_validator("telegram_api_id", mode="before")
    @classmethod
    def _blank_int(cls, v: object) -> object:
        # An unset `TELEGRAM_API_ID=` in .env arrives as "" — treat it as 0.
        if isinstance(v, str) and v.strip() == "":
            return 0
        return v

    telegram_api_hash: str = ""
    telegram_session: str = ""

    # GitHub (seed values; the dashboard may override and persist them in the DB)
    github_token: str = ""
    github_repository: str = ""
    github_branch: str = "main"
    github_target_dir: str = ""

    # Storage
    data_dir: str = "/data"

    # Optional dashboard HTTP basic auth.
    # Prefer DASHBOARD_PASSWORD_HASH (salted PBKDF2, see app.core.security); the
    # plaintext DASHBOARD_PASSWORD is kept only as a fallback for older setups.
    dashboard_user: str = ""
    dashboard_password: str = ""
    dashboard_password_hash: str = ""

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_path / "v2get.db"

    @property
    def db_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.db_path}"

    @property
    def output_dir(self) -> Path:
        return self.data_path / "output"

    @property
    def blacklist_dir(self) -> Path:
        return self.data_path / "blacklist"

    @property
    def logs_dir(self) -> Path:
        return self.data_path / "logs"

    @property
    def session_dir(self) -> Path:
        return self.data_path / "session"

    def ensure_dirs(self) -> None:
        for p in (
            self.data_path,
            self.output_dir,
            self.blacklist_dir,
            self.logs_dir,
            self.session_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_config() -> EnvConfig:
    cfg = EnvConfig()
    # Allow DATA_DIR to override even when .env present.
    if os.getenv("DATA_DIR"):
        cfg.data_dir = os.environ["DATA_DIR"]
    return cfg


config = get_config()
