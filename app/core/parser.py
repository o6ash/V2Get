"""Robust extraction of proxy configs from arbitrary message text.

Handles multiple links per message, links embedded in prose, and mixed
protocols. Each recognised link is normalised into a :class:`ParsedConfig`
carrying the structured fields needed for fingerprinting and TCP validation.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, unquote, urlsplit

SUPPORTED = ("vmess", "vless", "trojan", "ss", "ssr", "hysteria2", "hy2", "tuic")

# Matches any supported scheme up to the first whitespace. Telegram messages
# frequently glue links to surrounding text, so we capture greedily then trim.
_LINK_RE = re.compile(
    r"(?:vmess|vless|trojan|ss|ssr|hysteria2|hy2|tuic)://[^\s`'\"<>]+",
    re.IGNORECASE,
)


@dataclass(slots=True)
class ParsedConfig:
    protocol: str            # normalised: vmess|vless|trojan|ss|ssr|hysteria2|tuic
    raw: str                 # the original (cleaned) link
    host: str = ""
    port: int = 0
    uuid: str = ""           # uuid / id (vmess, vless, tuic)
    password: str = ""       # password / auth (trojan, ss, hy2, tuic)
    method: str = ""         # cipher (ss, ssr)
    name: str = ""           # remark / tag — never part of the fingerprint
    extra: dict = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return bool(self.host) and 0 < self.port < 65536


def _b64decode(data: str) -> bytes:
    data = data.strip().replace("-", "+").replace("_", "/")
    pad = len(data) % 4
    if pad:
        data += "=" * (4 - pad)
    return base64.b64decode(data)


def _normalise_protocol(scheme: str) -> str:
    scheme = scheme.lower()
    return "hysteria2" if scheme == "hy2" else scheme


def extract_links(text: str) -> list[str]:
    if not text:
        return []
    return [m.group(0).rstrip(".,;") for m in _LINK_RE.finditer(text)]


def parse_link(link: str) -> ParsedConfig | None:
    link = link.strip()
    scheme = link.split("://", 1)[0].lower()
    try:
        if scheme == "vmess":
            return _parse_vmess(link)
        if scheme == "vless":
            return _parse_vless(link)
        if scheme == "trojan":
            return _parse_trojan(link)
        if scheme == "ss":
            return _parse_ss(link)
        if scheme == "ssr":
            return _parse_ssr(link)
        if scheme in ("hysteria2", "hy2"):
            return _parse_hysteria2(link)
        if scheme == "tuic":
            return _parse_tuic(link)
    except (ValueError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return None


def rename(raw: str, new_name: str) -> str:
    """Return ``raw`` with its display name/remark replaced by ``new_name``.

    Each protocol stores the remark differently: vmess in the base64-JSON ``ps``
    field, the URL-style schemes (vless/trojan/ss/hysteria2/tuic) in the URI
    ``#fragment``. ssr and anything unrecognised or malformed is returned
    unchanged — a rename must never corrupt or drop a working config.
    """
    scheme = raw.split("://", 1)[0].lower()
    try:
        if scheme == "vmess":
            payload = raw[len("vmess://"):]
            data = json.loads(_b64decode(payload).decode("utf-8", "ignore"))
            data["ps"] = new_name
            encoded = base64.b64encode(
                json.dumps(data, ensure_ascii=False).encode("utf-8")
            ).decode("ascii")
            return "vmess://" + encoded
        if scheme in ("vless", "trojan", "ss", "hysteria2", "hy2", "tuic"):
            base = raw.split("#", 1)[0]
            return f"{base}#{quote(new_name, safe='')}"
    except (ValueError, binascii.Error, json.JSONDecodeError, UnicodeDecodeError):
        return raw
    return raw


def parse_text(text: str) -> list[ParsedConfig]:
    out: list[ParsedConfig] = []
    for link in extract_links(text):
        cfg = parse_link(link)
        if cfg and cfg.valid:
            out.append(cfg)
    return out


# ── per-protocol parsers ──────────────────────────────────────────────────────

def _parse_vmess(link: str) -> ParsedConfig | None:
    payload = link[len("vmess://"):]
    data = json.loads(_b64decode(payload).decode("utf-8", "ignore"))
    return ParsedConfig(
        protocol="vmess",
        raw=link,
        host=str(data.get("add", "")).strip(),
        port=int(data.get("port", 0) or 0),
        uuid=str(data.get("id", "")).strip(),
        name=str(data.get("ps", "")),
        extra={k: data.get(k) for k in ("net", "tls", "host", "path", "type")},
    )


def _split_userinfo(parts) -> tuple[str, str, int, str]:
    """Return (userinfo, host, port, fragment-name) from a urlsplit result."""
    host = parts.hostname or ""
    port = parts.port or 0
    name = unquote(parts.fragment) if parts.fragment else ""
    userinfo = parts.username or ""
    return userinfo, host, port, name


def _parse_vless(link: str) -> ParsedConfig | None:
    parts = urlsplit(link)
    userinfo, host, port, name = _split_userinfo(parts)
    q = parse_qs(parts.query)
    return ParsedConfig(
        protocol="vless",
        raw=link,
        host=host,
        port=int(port),
        uuid=unquote(userinfo),
        name=name,
        extra={k: v[0] for k, v in q.items()},
    )


def _parse_trojan(link: str) -> ParsedConfig | None:
    parts = urlsplit(link)
    userinfo, host, port, name = _split_userinfo(parts)
    return ParsedConfig(
        protocol="trojan",
        raw=link,
        host=host,
        port=int(port),
        password=unquote(userinfo),
        name=name,
    )


def _parse_ss(link: str) -> ParsedConfig | None:
    body = link[len("ss://"):]
    name = ""
    if "#" in body:
        body, frag = body.split("#", 1)
        name = unquote(frag)

    method = password = host = ""
    port = 0
    if "@" in body:
        # ss://method:password@host:port  (creds may themselves be base64)
        creds, server = body.rsplit("@", 1)
        if ":" not in creds:
            creds = _b64decode(creds).decode("utf-8", "ignore")
        method, _, password = creds.partition(":")
        host, _, port_s = server.partition(":")
        port = int(re.sub(r"[^0-9].*$", "", port_s) or 0)
    else:
        # ss://base64(method:password@host:port)
        decoded = _b64decode(body).decode("utf-8", "ignore")
        creds, _, server = decoded.partition("@")
        method, _, password = creds.partition(":")
        host, _, port_s = server.partition(":")
        port = int(port_s or 0)
    return ParsedConfig(
        protocol="ss", raw=link, host=host, port=port,
        method=method, password=password, name=name,
    )


def _parse_ssr(link: str) -> ParsedConfig | None:
    payload = link[len("ssr://"):]
    decoded = _b64decode(payload).decode("utf-8", "ignore")
    main, _, _ = decoded.partition("/?")
    fields = main.split(":")
    if len(fields) < 6:
        return None
    host, port, _proto, method, _obfs, pwd_b64 = fields[:6]
    password = _b64decode(pwd_b64).decode("utf-8", "ignore") if pwd_b64 else ""
    return ParsedConfig(
        protocol="ssr", raw=link, host=host, port=int(port or 0),
        method=method, password=password,
    )


def _parse_hysteria2(link: str) -> ParsedConfig | None:
    parts = urlsplit(link)
    userinfo, host, port, name = _split_userinfo(parts)
    q = parse_qs(parts.query)
    return ParsedConfig(
        protocol="hysteria2",
        raw=link,
        host=host,
        port=int(port or 0),
        password=unquote(userinfo) or q.get("auth", [""])[0],
        name=name,
        extra={k: v[0] for k, v in q.items()},
    )


def _parse_tuic(link: str) -> ParsedConfig | None:
    parts = urlsplit(link)
    host = parts.hostname or ""
    port = parts.port or 0
    name = unquote(parts.fragment) if parts.fragment else ""
    uuid = unquote(parts.username or "")
    password = unquote(parts.password or "")
    q = parse_qs(parts.query)
    return ParsedConfig(
        protocol="tuic",
        raw=link,
        host=host,
        port=int(port or 0),
        uuid=uuid,
        password=password,
        name=name,
        extra={k: v[0] for k, v in q.items()},
    )
