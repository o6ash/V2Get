"""High-level operations on NapsternetV / NPV Tunnel ``.npvt`` files.

A ``.npvt`` file is::

    NPVT1\\n<blob0>,<blob1>,<blob2>

where each ``<blobN>`` is ``base64(nonce[16] || ciphertext)`` under whitebox
AES-CTR (see :mod:`npvt_crypto`). Decrypted, the blobs are: an ASCII profile
count, the profiles JSON array, and a trailing top-level lock object.

This module stays free of any Telegram dependency so it can be unit-tested on
its own.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from app.npvt.unlocker.npvt_crypto import (
    b64_decode_token,
    b64_encode_blob,
    decrypt_blob,
    encrypt_blob,
)

HEADER = b"NPVT1"

# Fields that constitute a "lock" inside any lockConfig-shaped dict.
_LOCK_NEUTRALIZE = {
    "isLocked": False,
    "blockRootedAndJailbroken": False,
    "message": "",
}


class NpvtError(ValueError):
    """Raised when a file is not a well-formed NPVT1 container."""


@dataclass
class Blob:
    nonce: bytes
    plaintext: bytes

    @property
    def raw(self) -> bytes:
        return encrypt_blob(self.nonce, self.plaintext)

    def json(self):
        """Parsed JSON if the plaintext is JSON, else ``None``."""
        try:
            return json.loads(self.plaintext.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def set_json(self, obj) -> None:
        self.plaintext = json.dumps(
            obj, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")


class Npvt:
    """A parsed, decrypted ``.npvt`` container."""

    def __init__(self, blobs: list[Blob]):
        self.blobs = blobs

    # --- (de)serialization ---------------------------------------------------

    @classmethod
    def parse(cls, data: bytes) -> "Npvt":
        if b"\n" not in data:
            raise NpvtError("missing NPVT1 header line")
        header, body = data.split(b"\n", 1)
        if header.strip() != HEADER:
            raise NpvtError(f"unsupported header {header!r}; expected {HEADER!r}")
        tokens = [t for t in body.decode("utf-8", "replace").split(",") if t.strip()]
        if not tokens:
            raise NpvtError("no config blobs found")
        blobs: list[Blob] = []
        for tok in tokens:
            raw = b64_decode_token(tok)
            if len(raw) < 16:
                raise NpvtError("blob shorter than a 16-byte nonce")
            blobs.append(Blob(nonce=raw[:16], plaintext=decrypt_blob(raw)))
        return cls(blobs)

    def serialize(self) -> bytes:
        body = ",".join(b64_encode_blob(b.raw) for b in self.blobs)
        return HEADER + b"\n" + body.encode("ascii")

    # --- profile access ------------------------------------------------------

    def profiles(self) -> list[dict]:
        """The list of profile dicts (the JSON-array blob), or ``[]``."""
        for b in self.blobs:
            v = b.json()
            if isinstance(v, list):
                return v
        return []

    def _lock_dicts(self):
        """Yield every lockConfig-shaped dict across all blobs (nested too)."""
        for b in self.blobs:
            v = b.json()
            if v is None:
                continue
            yield from _walk_lock_dicts(v)

    # --- transforms ----------------------------------------------------------

    def unlock(self) -> "Npvt":
        """Neutralize every lock block. Mutates and returns self."""
        for b in self.blobs:
            v = b.json()
            if v is None:
                continue
            changed = False
            for d in _walk_lock_dicts(v):
                for k, val in _LOCK_NEUTRALIZE.items():
                    if k in d and d[k] != val:
                        d[k] = val
                        changed = True
            if changed:
                b.set_json(v)
        return self

    def lock(self, password: str = "", message: str = "",
             block_rooted: bool = False) -> "Npvt":
        """Set a lock on every lock block. Mutates and returns self."""
        for b in self.blobs:
            v = b.json()
            if v is None:
                continue
            changed = False
            for d in _walk_lock_dicts(v):
                d["isLocked"] = True
                d["password"] = password
                d["message"] = message
                d["blockRootedAndJailbroken"] = block_rooted
                changed = True
            if changed:
                b.set_json(v)
        return self

    def is_locked(self) -> bool:
        return any(d.get("isLocked") for d in self._lock_dicts())

    # --- reporting -----------------------------------------------------------

    def summary(self) -> str:
        profs = self.profiles()
        if not profs:
            return "No profiles found in this config."
        lines = [f"{len(profs)} profile(s):"]
        for i, p in enumerate(profs, 1):
            v2 = p.get("v2rayProfile", {})
            proto = _outbound_protocol(v2) or _guess_protocol(v2) or p.get("type", "?")
            server = v2.get("server") or p.get("address", "?")
            port = v2.get("serverPort", "")
            host = v2.get("host", "")
            sni = v2.get("sni", "")
            net = v2.get("network", "")
            sec = v2.get("security", "")
            locked = "🔒" if p.get("lockConfig", {}).get("isLocked") else "🔓"
            lines.append(
                f"\n{i}. {locked} {p.get('name','(unnamed)')}\n"
                f"   {proto} · {server}:{port} · {net}/{sec}\n"
                f"   host={host or '-'}  sni={sni or '-'}"
            )
        return "\n".join(lines)

    def to_uris(self) -> list[str]:
        uris: list[str] = []
        for p in self.profiles():
            uri = _profile_to_uri(p)
            if uri:
                uris.append(uri)
        return uris


# --- helpers -----------------------------------------------------------------

def _walk_lock_dicts(node):
    """Yield any dict that looks like a lockConfig (has an ``isLocked`` key)."""
    if isinstance(node, dict):
        if "isLocked" in node:
            yield node
        for val in node.values():
            yield from _walk_lock_dicts(val)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_lock_dicts(item)


def _outbound(v2: dict) -> dict:
    """The proxy outbound object from an embedded v2rayJson, or {}."""
    raw = v2.get("v2rayJson")
    if not raw:
        return {}
    try:
        cfg = json.loads(raw)
    except ValueError:
        return {}
    for ob in cfg.get("outbounds", []):
        if ob.get("tag") == "proxy" or ob.get("protocol") in (
            "trojan", "vmess", "vless", "shadowsocks"
        ):
            return ob
    return {}


def _outbound_protocol(v2: dict) -> str:
    return _outbound(v2).get("protocol", "")


_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# NapsternetV's `configType` is its own protocol enum for structured profiles
# (those with no embedded v2rayJson). Confirmed against real configs:
#   3 -> shadowsocks,  4 -> socks,  5 -> vless,  6 -> trojan.
_CONFIG_TYPE_PROTO = {3: "shadowsocks", 4: "socks", 5: "vless", 6: "trojan"}

_PATH_NETS = ("ws", "httpupgrade", "http", "h2", "xhttp", "splithttp")
_HOST_NETS = ("ws", "httpupgrade", "http", "h2")


# vmess `security` values — these are NOT shadowsocks ciphers.
_VMESS_SCY = {"auto", "aes-128-gcm", "chacha20-poly1305", "none", "zero"}


def _guess_protocol(v2: dict) -> str | None:
    """Infer the protocol for a structured profile (no v2rayJson).

    ``configType`` is authoritative when known; otherwise fall back to the
    shape of the credential. Note ``method`` is often the literal string
    ``"none"`` (meaning "unset"), which must not be read as shadowsocks.
    """
    ct = v2.get("configType")
    if ct in _CONFIG_TYPE_PROTO:
        return _CONFIG_TYPE_PROTO[ct]
    method = (v2.get("method") or "").strip().lower()
    pwd = v2.get("password") or ""
    if _UUID_RE.match(pwd):
        # UUID credential -> vmess or vless. A vmess `security` (auto,
        # aes-128-gcm, chacha20-poly1305, zero) means vmess; else vless.
        return "vmess" if method in _VMESS_SCY and method != "none" else "vless"
    if method and method != "none":
        return "shadowsocks"  # non-UUID credential + real cipher
    if pwd:
        return "trojan"
    return None


def _first(*vals) -> str:
    for v in vals:
        if v:
            return str(v)
    return ""


def _extract(p: dict) -> dict:
    """Unified parameters, preferring a v2rayJson outbound then structured fields."""
    v2 = p.get("v2rayProfile", {})
    ob = _outbound(v2)
    ss = ob.get("streamSettings", {})
    settings = ob.get("settings", {})
    servers = settings.get("servers", [])
    vnext = settings.get("vnext", [])
    node = vnext[0] if vnext else (servers[0] if servers else {})
    user = (node.get("users") or [{}])[0] if vnext else {}

    tls = ss.get("tlsSettings") or ss.get("realitySettings") or {}
    ws = ss.get("wsSettings") or ss.get("httpupgradeSettings") or {}
    grpc = ss.get("grpcSettings") or {}
    xhttp = ss.get("xhttpSettings") or ss.get("splithttpSettings") or {}
    tcp_hdr = (ss.get("tcpSettings", {}) or {}).get("header", {}) or {}

    alpn = tls.get("alpn") or v2.get("alpn") or ""
    if isinstance(alpn, list):
        alpn = ",".join(alpn)
    insecure = tls.get("allowInsecure", v2.get("insecure", False))

    return {
        "proto": ob.get("protocol") or _guess_protocol(v2),
        # Names often carry stray padding in real configs; the fragment is a
        # display label, so trim it.
        "name": (p.get("name") or "").strip(),
        "addr": _first(node.get("address"), v2.get("server")),
        "port": _first(node.get("port"), v2.get("serverPort")),
        "pwd": _first(node.get("password"), user.get("id"), v2.get("password")),
        "net": _first(ss.get("network"), v2.get("network"), "tcp"),
        "sec": _first(ss.get("security"), v2.get("security"), "none"),
        "host": _first((ws.get("headers", {}) or {}).get("Host"), ws.get("host"), v2.get("host")),
        "path": _first(ws.get("path"), xhttp.get("path"), v2.get("path")),
        "service": _first(grpc.get("serviceName"), v2.get("serviceName")),
        "user": _first(node.get("user"), v2.get("username")),
        "mode": _first(xhttp.get("mode"), v2.get("mode"), v2.get("xhttpMode")),
        "sni": _first(tls.get("serverName"), v2.get("sni")),
        "alpn": alpn,
        "fp": _first(tls.get("fingerprint"), v2.get("fingerPrint")),
        "pbk": _first(tls.get("publicKey"), v2.get("publicKey")),
        "sid": _first(tls.get("shortId"), v2.get("shortId")),
        "spx": _first(tls.get("spiderX"), v2.get("spiderX")),
        "flow": _first(user.get("flow"), v2.get("flow")),
        "header": _first(tcp_hdr.get("type"), v2.get("headerType")),
        "enc": _first(v2.get("method"), user.get("encryption"), "none"),
        # ss cipher / vmess scy: prefer the v2rayJson node, then structured field
        "method": _first(node.get("method"), v2.get("method")),
        "insecure": "1" if insecure else "0",
    }


# WebSocket and httpupgrade are HTTP/1.1 upgrade mechanisms: advertising the
# HTTP/2 ALPN token makes the server (notably Cloudflare) negotiate h2, and the
# upgrade then fails. Strip h2 from those transports; leave h2 alone on grpc /
# xhttp / tcp, which legitimately use it.
_H1_ONLY_NETS = ("ws", "httpupgrade")


def _clean_alpn(alpn: str, net: str) -> str:
    if alpn and net in _H1_ONLY_NETS:
        alpn = ",".join(t for t in alpn.split(",") if t.strip().lower() != "h2")
    return alpn


def _common_query(x: dict, *, encryption: bool, flow: bool) -> dict:
    """Stream-settings query shared by vless/trojan, in the reference order."""
    q: dict = {}
    if encryption:
        q["encryption"] = x["enc"]
    q["type"] = x["net"]
    q["security"] = x["sec"]
    if x["header"]:
        q["headerType"] = x["header"]
    if x["path"] and x["net"] in _PATH_NETS:
        q["path"] = x["path"]
    if x["host"] and x["net"] in _HOST_NETS:
        q["host"] = x["host"]
    if x["mode"] and x["net"] in ("xhttp", "splithttp"):
        q["mode"] = x["mode"]
    if x["service"] and x["net"] == "grpc":
        q["serviceName"] = x["service"]
    if x["sni"]:
        q["sni"] = x["sni"]
    alpn = _clean_alpn(x["alpn"], x["net"])
    if alpn and x["sec"] != "reality":  # reality links omit alpn
        q["alpn"] = alpn
    if x["fp"]:
        q["fp"] = x["fp"]
    q["insecure"] = x["insecure"]
    q["allowInsecure"] = x["insecure"]
    if x["pbk"]:
        q["pbk"] = x["pbk"]
    if x["sid"]:
        q["sid"] = x["sid"]
    if x["spx"]:
        q["spx"] = x["spx"]
    if flow and x["flow"]:
        q["flow"] = x["flow"]
    return q


def _profile_to_uri(p: dict) -> str | None:
    """Build a standard share URI from a profile (v2rayJson or structured)."""
    x = _extract(p)
    proto = x["proto"]
    if not proto or not x["addr"]:
        return None
    name = quote(x["name"])

    if proto == "vless":
        q = _common_query(x, encryption=True, flow=True)
        # Usually a bare UUID (quoting is a no-op), but some configs stuff an
        # ad string with spaces in here, which would break the URI.
        uid = quote(str(x["pwd"]), safe="")
        return f"vless://{uid}@{x['addr']}:{x['port']}?{urlencode(q)}#{name}"

    if proto == "trojan":
        q = _common_query(x, encryption=False, flow=False)
        return f"trojan://{quote(str(x['pwd']))}@{x['addr']}:{x['port']}?{urlencode(q)}#{name}"

    if proto == "socks":
        # A plain proxy: host:port is the whole config. Credentials are
        # optional and, when present, go in as base64("user:pass").
        userinfo = ""
        if x["user"] or x["pwd"]:
            import base64 as _b64
            raw = f"{x['user'] or ''}:{x['pwd'] or ''}".encode()
            userinfo = _b64.b64encode(raw).decode().rstrip("=") + "@"
        return f"socks://{userinfo}{x['addr']}:{x['port']}#{name}"

    if proto == "shadowsocks":
        import base64 as _b64
        method = x["method"].strip().lower()
        if method in ("", "none", "auto"):  # not a real ss cipher -> unusable
            return None
        userinfo = _b64.b64encode(f"{method}:{x['pwd']}".encode()).decode().rstrip("=")
        return f"ss://{userinfo}@{x['addr']}:{x['port']}#{name}"

    if proto == "vmess":
        import base64 as _b64
        vmess_obj = {
            "v": "2", "ps": x["name"], "add": x["addr"], "port": str(x["port"]),
            "id": x["pwd"], "aid": "0", "scy": x["method"] or "auto",
            "net": x["net"], "type": x["header"] or "none", "host": x["host"],
            "path": x["path"], "tls": x["sec"], "sni": x["sni"],
            "alpn": x["alpn"], "fp": x["fp"],
        }
        payload = _b64.b64encode(
            json.dumps(vmess_obj, ensure_ascii=False).encode()
        ).decode()
        return f"vmess://{payload}"

    return None
