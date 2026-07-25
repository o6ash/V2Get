"""Password hashing for the dashboard login.

HTTP Basic auth transmits ``base64(user:password)`` — that is an *encoding*, not
encryption, so a stored plaintext password (and the on-the-wire credential) is
trivially recoverable. We never store the password itself: only a salted,
iterated PBKDF2-HMAC-SHA256 digest, verified in constant time. Put the digest in
``DASHBOARD_PASSWORD_HASH``; generate one with ``python -m app.tools.hash_password``.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 200_000
_SALT_BYTES = 16


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def hash_password(password: str, *, iterations: int = _ITERATIONS) -> str:
    """Return an encoded ``algo$iterations$salt$digest`` string for ``password``."""
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"{_ALGO}${iterations}${_b64e(salt)}${_b64e(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of ``password`` against an encoded PBKDF2 hash."""
    try:
        algo, iters, salt_b64, digest_b64 = encoded.split("$")
        if algo != _ALGO:
            return False
        salt = _b64d(salt_b64)
        expected = _b64d(digest_b64)
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iters)
        )
    except (ValueError, binascii.Error):
        return False
    return hmac.compare_digest(candidate, expected)
