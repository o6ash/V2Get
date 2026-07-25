"""Identity fingerprints used for intelligent deduplication.

Fingerprints are derived from the *connection-defining* fields only, so two
configs that point at the same endpoint with the same credentials collapse to
one identity regardless of their name, remarks, tags or query parameters.

    vmess / vless / tuic : server + port + uuid
    trojan / hysteria2   : server + port + password
    ss / ssr             : server + port + method + password
"""
from __future__ import annotations

import hashlib

from app.core.parser import ParsedConfig


def fingerprint(cfg: ParsedConfig) -> str:
    host = cfg.host.strip().lower()
    port = str(cfg.port)

    if cfg.protocol in ("vmess", "vless", "tuic"):
        secret = cfg.uuid.strip()
    elif cfg.protocol in ("trojan", "hysteria2"):
        secret = cfg.password.strip()
    elif cfg.protocol in ("ss", "ssr"):
        secret = f"{cfg.method.strip()}:{cfg.password.strip()}"
    else:
        secret = cfg.uuid or cfg.password

    material = f"{cfg.protocol}|{host}|{port}|{secret}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"{cfg.protocol}:{digest}"
