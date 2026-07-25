"""File-backed blacklists with hot reload (domains, IPs, keywords).

Entries live in plain text files on the volume so they can be edited, imported
and exported from the dashboard and are reloaded on every access without a
restart. Files are re-read only when their mtime changes.
"""
from __future__ import annotations

import ipaddress
from pathlib import Path

from app.config import config
from app.core.parser import ParsedConfig

_KINDS = ("domains", "ips", "keywords")
_DEFAULT_DOMAINS = ["localhost"]
_DEFAULT_IPS = ["127.0.0.1", "0.0.0.0", "::1"]
_DEFAULT_KEYWORDS: list[str] = []


class Blacklist:
    def __init__(self) -> None:
        self._cache: dict[str, set[str]] = {k: set() for k in _KINDS}
        self._mtimes: dict[str, float] = {}

    def _path(self, kind: str) -> Path:
        return config.blacklist_dir / f"blacklist_{kind}.txt"

    def ensure_files(self) -> None:
        config.ensure_dirs()
        defaults = {
            "domains": _DEFAULT_DOMAINS,
            "ips": _DEFAULT_IPS,
            "keywords": _DEFAULT_KEYWORDS,
        }
        for kind in _KINDS:
            p = self._path(kind)
            if not p.exists():
                p.write_text("\n".join(defaults[kind]) + ("\n" if defaults[kind] else ""))

    def _reload(self, kind: str) -> None:
        p = self._path(kind)
        if not p.exists():
            self._cache[kind] = set()
            return
        mtime = p.stat().st_mtime
        if self._mtimes.get(kind) == mtime:
            return
        lines = {
            ln.strip().lower()
            for ln in p.read_text(encoding="utf-8", errors="ignore").splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        }
        self._cache[kind] = lines
        self._mtimes[kind] = mtime

    def get(self, kind: str) -> list[str]:
        self._reload(kind)
        return sorted(self._cache[kind])

    def all(self) -> dict[str, list[str]]:
        return {k: self.get(k) for k in _KINDS}

    def add(self, kind: str, entry: str) -> None:
        self._mutate(kind, add={entry.strip().lower()})

    def remove(self, kind: str, entry: str) -> None:
        self._mutate(kind, remove={entry.strip().lower()})

    def replace(self, kind: str, entries: list[str]) -> None:
        cleaned = {e.strip().lower() for e in entries if e.strip()}
        self._write(kind, cleaned)

    def _mutate(self, kind: str, add: set[str] | None = None, remove: set[str] | None = None) -> None:
        self._reload(kind)
        current = set(self._cache[kind])
        if add:
            current |= {a for a in add if a}
        if remove:
            current -= remove
        self._write(kind, current)

    def _write(self, kind: str, entries: set[str]) -> None:
        if kind not in _KINDS:
            raise ValueError(f"unknown blacklist kind: {kind}")
        p = self._path(kind)
        p.write_text("\n".join(sorted(entries)) + ("\n" if entries else ""))
        self._mtimes.pop(kind, None)  # force reload next read

    # matching --------------------------------------------------------------------
    def is_blocked(self, cfg: ParsedConfig) -> tuple[bool, str]:
        host = cfg.host.strip().lower()
        domains = set(self.get("domains"))
        ips = set(self.get("ips"))
        keywords = self.get("keywords")

        if host in domains or any(host == d or host.endswith("." + d) for d in domains):
            return True, f"domain:{host}"

        if _is_ip(host) and host in ips:
            return True, f"ip:{host}"

        haystack = f"{cfg.raw} {cfg.name}".lower()
        for kw in keywords:
            if kw and kw in haystack:
                return True, f"keyword:{kw}"
        return False, ""


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


blacklist = Blacklist()
