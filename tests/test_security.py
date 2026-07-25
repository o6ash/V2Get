"""Dashboard password hashing."""
from __future__ import annotations

from app.core.security import hash_password, verify_password


def test_roundtrip_accepts_the_correct_password():
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded)


def test_wrong_password_is_rejected():
    assert not verify_password("wrong", hash_password("right"))


def test_hash_is_salted_so_the_same_password_hashes_differently():
    a = hash_password("same")
    b = hash_password("same")
    assert a != b
    assert verify_password("same", a) and verify_password("same", b)


def test_encoded_format_is_self_describing():
    algo, iterations, salt, digest = hash_password("x", iterations=1000).split("$")
    assert algo == "pbkdf2_sha256"
    assert int(iterations) == 1000
    assert salt and digest


def test_plaintext_password_never_appears_in_the_hash():
    secret = "SuperSecret123"
    assert secret not in hash_password(secret)


def test_malformed_hashes_are_rejected_rather_than_crashing():
    for junk in ("", "nonsense", "a$b$c", "pbkdf2_sha256$notanint$s$d", "$$$"):
        assert verify_password("x", junk) is False


def test_unknown_algorithm_is_rejected():
    encoded = hash_password("x")
    tampered = encoded.replace("pbkdf2_sha256", "md5_lol", 1)
    assert not verify_password("x", tampered)
