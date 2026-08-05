"""Minimal .ovpn config parsing + local storage.

Only what the pipeline needs: validate that the payload really is an OpenVPN
profile, extract the first ``remote`` endpoint (host/port/proto) for the health
check, and write the file under ``<output_dir>/ovpn`` with a collision-free
name. Nothing here touches the core pipeline.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from app.config import config

_REMOTE_RE = re.compile(r"^\s*remote\s+(\S+)(?:\s+(\d+))?(?:\s+(tcp|udp)\w*)?", re.I | re.M)
_PROTO_RE = re.compile(r"^\s*proto\s+(tcp|udp)\w*", re.I | re.M)
_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")

# A file must look like an OpenVPN profile, not an arbitrary text blob.
_MARKERS = ("remote ", "client", "dev tun", "dev tap", "<ca>", "proto ")

OVPN_DIRNAME = "ovpn"


class ParseError(ValueError):
    """Payload is not a usable OpenVPN profile."""


@dataclass(slots=True)
class OvpnProfile:
    text: str
    host: str
    port: int
    proto: str
    content_hash: str


def parse(data: bytes) -> OvpnProfile:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", "ignore")
    low = text.lower()
    if not any(m in low for m in _MARKERS):
        raise ParseError("not an OpenVPN profile")

    m = _REMOTE_RE.search(text)
    if not m:
        raise ParseError("no remote directive")
    host = m.group(1).strip()
    port = int(m.group(2)) if m.group(2) else 1194
    proto = (m.group(3) or "").lower()
    if not proto:
        pm = _PROTO_RE.search(text)
        proto = pm.group(1).lower() if pm else "udp"
    if not host or not (0 < port < 65536):
        raise ParseError("invalid remote endpoint")

    digest = hashlib.sha256(text.strip().encode("utf-8")).hexdigest()
    return OvpnProfile(text=text, host=host, port=port, proto=proto, content_hash=digest)


def stored_name(channel: str, message_id: int, file_name: str) -> str:
    """Collision-free, URL-safe name for the published file."""
    stem = _SAFE_RE.sub("-", (file_name or "config").rsplit("/", 1)[-1])
    if stem.lower().endswith(".ovpn"):
        stem = stem[:-5]
    stem = stem.strip("-._")[:48] or "config"
    chan = _SAFE_RE.sub("-", channel).strip("-._")[:32] or "src"
    return f"{chan}-{message_id}-{stem}.ovpn"


def ovpn_dir():
    d = config.output_dir / OVPN_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_local(name: str, text: str) -> None:
    (ovpn_dir() / name).write_text(text, encoding="utf-8")


def read_local(name: str) -> str | None:
    p = ovpn_dir() / name
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8", errors="ignore")
