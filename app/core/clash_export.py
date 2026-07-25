"""Translate raw proxy URIs into Clash / Stash YAML.

Unlike the old passthrough that dumped links as YAML comments, this produces a
*usable* configuration: every supported link is translated into a real Clash
proxy object, wired into two proxy-groups — a manual **🚀 Select** and a
latency-tested **♻️ Auto Select** — and routed through a rule set that sends all
Iranian (and private/LAN) destinations straight to ``DIRECT``.

Stash is Clash-compatible; the same document serves both, so Stash users also
get the Select / Auto Select pair. The Stash export is additionally filtered:
ss and hysteria2 proxies are excluded (they cause compatibility issues in
Stash), and only transports Stash officially supports (tcp/ws/grpc/h2/http) are
emitted — anything exotic such as xhttp is dropped rather than shipped broken.
"""
from __future__ import annotations

import json
import logging
import uuid as _uuid
from urllib.parse import parse_qs, unquote, urlsplit

import yaml

from app.core.parser import _b64decode, _parse_ss

log = logging.getLogger("v2get")

SELECT_GROUP = "🚀 Select"
AUTO_GROUP = "♻️ Auto Select"

# Iranian destinations bypass the proxy. GEOIP,IR is matched against the
# resolved IP (no ``no-resolve``), so Iran-hosted domains are caught too; every
# Clash/Stash client ships the GeoIP database these reference.
ROUTING_RULES = [
    "GEOIP,private,DIRECT,no-resolve",
    "GEOIP,IR,DIRECT",
    f"MATCH,{SELECT_GROUP}",
]

# A minimal DNS block so domain rules can resolve for GEOIP matching.
_DNS = {
    "enable": True,
    "ipv6": False,
    "nameserver": ["1.1.1.1", "8.8.8.8", "9.9.9.9"],
}


def _truthy(value: object) -> bool:
    return str(value).lower() in ("1", "true", "tls", "reality", "yes", "on")


def _valid_uuid(value: object) -> bool:
    """True when *value* is a well-formed UUID Stash/Clash will accept.

    vmess/vless/tuic carry a UUID; Stash's Go core parses it strictly and
    rejects the *entire* profile with "invalid UUID length" when a single node
    has a malformed one (empty, truncated, a placeholder like ``your-uuid``,
    etc.). We mirror that parser so a bad node is dropped instead of poisoning
    the whole export.
    """
    s = str(value).strip()
    try:
        _uuid.UUID(s)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _vmess(raw: str) -> dict | None:
    data = json.loads(_b64decode(raw[len("vmess://"):]).decode("utf-8", "ignore"))
    server = str(data.get("add", "")).strip()
    port = int(data.get("port", 0) or 0)
    if not server or not port:
        return None
    net = str(data.get("net", "tcp") or "tcp").lower()
    if net == "raw":  # Xray renamed plain TCP to "raw"; Stash/Clash say "tcp".
        net = "tcp"
    proxy: dict = {
        "name": str(data.get("ps", "")),
        "type": "vmess",
        "server": server,
        "port": port,
        "uuid": str(data.get("id", "")).strip(),
        "alterId": int(data.get("aid", 0) or 0),
        "cipher": str(data.get("scy") or "auto"),
        "udp": True,
        "network": net,
    }
    if _truthy(data.get("tls", "")):
        proxy["tls"] = True
        sni = data.get("sni") or data.get("host") or ""
        if sni:
            proxy["servername"] = str(sni)
        proxy["skip-cert-verify"] = True
    if net == "ws":
        opts: dict = {"path": str(data.get("path") or "/")}
        host = data.get("host")
        if host:
            opts["headers"] = {"Host": str(host)}
        proxy["ws-opts"] = opts
    elif net == "grpc":
        proxy["grpc-opts"] = {"grpc-service-name": str(data.get("path") or "")}
    return proxy


def _vless(raw: str) -> dict | None:
    p = urlsplit(raw)
    if not p.hostname or not p.port:
        return None
    q = {k: v[0] for k, v in parse_qs(p.query).items()}
    net = q.get("type", "tcp").lower()
    if net == "raw":  # Xray renamed plain TCP to "raw"; Stash/Clash say "tcp".
        net = "tcp"
    security = q.get("security", "").lower()
    proxy: dict = {
        "name": unquote(p.fragment),
        "type": "vless",
        "server": p.hostname,
        "port": int(p.port),
        "uuid": unquote(p.username or ""),
        "udp": True,
        "network": net,
    }
    if q.get("flow"):
        proxy["flow"] = q["flow"]
    if security in ("tls", "reality"):
        proxy["tls"] = True
        if q.get("sni"):
            proxy["servername"] = q["sni"]
        if q.get("fp"):
            proxy["client-fingerprint"] = q["fp"]
        if q.get("alpn"):
            proxy["alpn"] = q["alpn"].split(",")
    if security == "reality":
        reality: dict = {}
        if q.get("pbk"):
            reality["public-key"] = q["pbk"]
        if q.get("sid"):
            reality["short-id"] = q["sid"]
        if reality:
            proxy["reality-opts"] = reality
    elif security != "tls":
        proxy["skip-cert-verify"] = True
    if net == "ws":
        opts: dict = {"path": q.get("path", "/")}
        if q.get("host"):
            opts["headers"] = {"Host": q["host"]}
        proxy["ws-opts"] = opts
    elif net == "grpc":
        proxy["grpc-opts"] = {"grpc-service-name": q.get("serviceName", "")}
        # Stash/Clash.Meta reject vless-grpc unless TLS is enabled ("TLS must be
        # true with vless-grpc"). gRPC rides HTTP/2, which the client only does
        # over TLS, so force it on even when the link omitted security=tls.
        if not proxy.get("tls"):
            proxy["tls"] = True
            if q.get("sni"):
                proxy["servername"] = q["sni"]
            if q.get("fp"):
                proxy["client-fingerprint"] = q["fp"]
            if q.get("alpn"):
                proxy["alpn"] = q["alpn"].split(",")
            # We forced TLS without an explicit security=tls, so the cert may not
            # validate (e.g. self-signed / IP host); keep cert checks relaxed.
            proxy.setdefault("skip-cert-verify", True)
    return proxy


def _trojan(raw: str) -> dict | None:
    p = urlsplit(raw)
    if not p.hostname or not p.port:
        return None
    q = {k: v[0] for k, v in parse_qs(p.query).items()}
    proxy: dict = {
        "name": unquote(p.fragment),
        "type": "trojan",
        "server": p.hostname,
        "port": int(p.port),
        "password": unquote(p.username or ""),
        "udp": True,
    }
    if q.get("sni"):
        proxy["sni"] = q["sni"]
    if q.get("alpn"):
        proxy["alpn"] = q["alpn"].split(",")
    if q.get("allowInsecure") in ("1", "true") or q.get("allow_insecure") in ("1", "true"):
        proxy["skip-cert-verify"] = True
    net = q.get("type", "").lower()
    if net == "ws":
        opts: dict = {"path": q.get("path", "/")}
        if q.get("host"):
            opts["headers"] = {"Host": q["host"]}
        proxy["network"] = "ws"
        proxy["ws-opts"] = opts
    elif net == "grpc":
        proxy["network"] = "grpc"
        proxy["grpc-opts"] = {"grpc-service-name": q.get("serviceName", "")}
    return proxy


# Shadowsocks ciphers Stash (and Clash-Meta) can actually initialise. A cipher
# outside this set makes Stash bail at startup with "cipher not supported" and
# refuse the *whole* profile, so we drop the offending proxy instead of emitting
# an entry that breaks the export. SS-2022 (``2022-blake3-*``) is supported and
# kept; only genuinely unknown/dropped ciphers are filtered out.
_SS_SUPPORTED_CIPHERS = {
    # AEAD
    "aes-128-gcm", "aes-192-gcm", "aes-256-gcm",
    "chacha20-ietf-poly1305", "xchacha20-ietf-poly1305",
    # stream
    "aes-128-cfb", "aes-192-cfb", "aes-256-cfb",
    "aes-128-ctr", "aes-192-ctr", "aes-256-ctr",
    "chacha20-ietf", "rc4-md5",
    # Shadowsocks 2022
    "2022-blake3-aes-128-gcm", "2022-blake3-aes-256-gcm",
    "2022-blake3-chacha20-poly1305",
    # no encryption
    "none",
}

# Aliases seen in the wild → the canonical name Stash expects. This is the
# "map it correctly" path: a valid cipher under a non-canonical spelling is
# rewritten rather than dropped.
_SS_CIPHER_ALIASES = {
    "chacha20-poly1305": "chacha20-ietf-poly1305",
    "xchacha20-poly1305": "xchacha20-ietf-poly1305",
    "aead_aes_128_gcm": "aes-128-gcm",
    "aead_aes_256_gcm": "aes-256-gcm",
    "aead_chacha20_poly1305": "chacha20-ietf-poly1305",
    "2022-blake3-chacha20-ietf-poly1305": "2022-blake3-chacha20-poly1305",
    "plain": "none",
}


def _ss(raw: str) -> dict | None:
    c = _parse_ss(raw)
    if c is None or not c.host or not c.port or not c.method:
        return None
    # Preserve the original cipher; only normalise case and known aliases so a
    # valid method under a non-canonical spelling still maps to what Stash wants.
    cipher = c.method.strip().lower()
    cipher = _SS_CIPHER_ALIASES.get(cipher, cipher)
    if cipher not in _SS_SUPPORTED_CIPHERS:
        log.warning(
            "ss %s:%s skipped — cipher %r not supported by Stash",
            c.host, c.port, c.method,
        )
        return None
    return {
        "name": c.name,
        "type": "ss",
        "server": c.host,
        "port": c.port,
        "cipher": cipher,
        "password": c.password,
        "udp": True,
    }


def _hysteria2(raw: str) -> dict | None:
    p = urlsplit(raw)
    if not p.hostname or not p.port:
        return None
    q = {k: v[0] for k, v in parse_qs(p.query).items()}
    proxy: dict = {
        "name": unquote(p.fragment),
        "type": "hysteria2",
        "server": p.hostname,
        "port": int(p.port),
        "password": unquote(p.username or "") or q.get("auth", ""),
        "udp": True,
    }
    if q.get("sni"):
        proxy["sni"] = q["sni"]
    if q.get("insecure") in ("1", "true") or q.get("allowInsecure") in ("1", "true"):
        proxy["skip-cert-verify"] = True
    if q.get("obfs"):
        proxy["obfs"] = q["obfs"]
        if q.get("obfs-password"):
            proxy["obfs-password"] = q["obfs-password"]
    return proxy


def _tuic(raw: str) -> dict | None:
    p = urlsplit(raw)
    if not p.hostname or not p.port:
        return None
    q = {k: v[0] for k, v in parse_qs(p.query).items()}
    proxy: dict = {
        "name": unquote(p.fragment),
        "type": "tuic",
        "server": p.hostname,
        "port": int(p.port),
        "uuid": unquote(p.username or ""),
        "password": unquote(p.password or ""),
        "udp": True,
    }
    if q.get("sni"):
        proxy["sni"] = q["sni"]
    if q.get("alpn"):
        proxy["alpn"] = q["alpn"].split(",")
    if q.get("congestion_control"):
        proxy["congestion-controller"] = q["congestion_control"]
    if q.get("allow_insecure") in ("1", "true") or q.get("insecure") in ("1", "true"):
        proxy["skip-cert-verify"] = True
    return proxy


_CONVERTERS = {
    "vmess": _vmess,
    "vless": _vless,
    "trojan": _trojan,
    "ss": _ss,
    "hysteria2": _hysteria2,
    "hy2": _hysteria2,
    "tuic": _tuic,
}

# --- Stash export filtering -------------------------------------------------
# Stash is Clash-compatible but the user keeps these protocols out of the Stash
# profile because they cause compatibility issues there. Note Stash itself does
# support ss/hysteria2; this is a deliberate per-client exclusion, not a
# capability limit. Keys are the ``type`` field emitted by the converters above.
STASH_EXCLUDED_TYPES = frozenset({"ss", "hysteria", "hysteria2"})

# Transports Stash officially supports for vmess/vless/trojan, per the Stash
# Wiki (stash.wiki/en/proxy-protocols/proxy-types): tcp, ws, grpc, h2/http.
# A proxy carrying any other transport — notably xhttp/splithttp — makes Stash
# reject the profile, so it is dropped from the Stash export entirely.
STASH_SUPPORTED_NETWORKS = frozenset({"tcp", "ws", "grpc", "h2", "http"})


def build_proxies(
    links: list[str],
    *,
    exclude_types: frozenset[str] = frozenset(),
    allowed_networks: frozenset[str] | None = None,
) -> list[dict]:
    """Translate raw URIs into Clash proxy dicts with unique, non-empty names.

    ``exclude_types`` drops proxies by ``type`` (e.g. ss / hysteria2 for Stash).
    ``allowed_networks``, when given, drops any proxy whose ``network`` transport
    is outside the set (e.g. xhttp), keeping the export to transports the target
    client understands. Proxies without a ``network`` key (tuic, plain trojan)
    are unaffected by the network filter.
    """
    proxies: list[dict] = []
    used: set[str] = set()
    for link in links:
        scheme = link.split("://", 1)[0].lower()
        convert = _CONVERTERS.get(scheme)
        if convert is None:
            continue
        try:
            proxy = convert(link)
        except (ValueError, KeyError, json.JSONDecodeError, UnicodeDecodeError):
            proxy = None
        if not proxy:
            continue
        # vmess/vless/tuic carry a UUID; a malformed one makes Stash reject the
        # whole profile ("invalid UUID length"). Drop just the offending node.
        if "uuid" in proxy and not _valid_uuid(proxy["uuid"]):
            log.warning(
                "dropping %s %s:%s — invalid UUID %r",
                proxy["type"], proxy["server"], proxy["port"], proxy.get("uuid"),
            )
            continue
        if proxy["type"] in exclude_types:
            log.info("dropping %s proxy — excluded from this export", proxy["type"])
            continue
        net = proxy.get("network")
        if allowed_networks is not None and net is not None and net not in allowed_networks:
            log.warning(
                "dropping %s %s:%s — transport %r unsupported by this client",
                proxy["type"], proxy["server"], proxy["port"], net,
            )
            continue
        name = (proxy.get("name") or "").strip() or f"{proxy['server']}:{proxy['port']}"
        unique = name
        n = 2
        while unique in used:
            unique = f"{name} #{n}"
            n += 1
        used.add(unique)
        proxy["name"] = unique
        proxies.append(proxy)
    return proxies


def _document(proxies: list[dict]) -> dict:
    names = [p["name"] for p in proxies]
    select = {
        "name": SELECT_GROUP,
        "type": "select",
        "proxies": [AUTO_GROUP, "DIRECT", *names],
    }
    auto = {
        "name": AUTO_GROUP,
        "type": "url-test",
        "url": "http://www.gstatic.com/generate_204",
        "interval": 300,
        "tolerance": 50,
        # url-test requires at least one member; fall back to DIRECT when empty.
        "proxies": names or ["DIRECT"],
    }
    return {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "dns": _DNS,
        "proxies": proxies,
        "proxy-groups": [select, auto],
        "rules": ROUTING_RULES,
    }


def _render(
    links: list[str],
    *,
    client: str,
    exclude_types: frozenset[str] = frozenset(),
    allowed_networks: frozenset[str] | None = None,
) -> str:
    proxies = build_proxies(
        links, exclude_types=exclude_types, allowed_networks=allowed_networks
    )
    header = (
        f"# {client} subscription generated by v2get\n"
        f"# {len(proxies)} proxies · Iran & private traffic routed DIRECT\n"
    )
    body = yaml.safe_dump(
        _document(proxies),
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=4096,
    )
    return header + body


def clash_yaml(links: list[str]) -> str:
    return _render(links, client="Clash")


def stash_yaml(links: list[str]) -> str:
    # Stash gets a filtered profile: no ss/hysteria2 (compatibility issues) and
    # only transports Stash officially supports (xhttp and friends dropped).
    return _render(
        links,
        client="Stash",
        exclude_types=STASH_EXCLUDED_TYPES,
        allowed_networks=STASH_SUPPORTED_NETWORKS,
    )
