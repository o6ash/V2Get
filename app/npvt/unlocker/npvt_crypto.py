"""Whitebox AES-CTR core for the NPVT1 (.npvt) format.

Ported line-for-line from the Pantegnos Go reference (modules/impl/npvt.go).
The "key" is not a byte string: it is folded into the lookup tables in
npvt_tables.py (TY_BOXES / MBL / TBOXES_LAST / XOR_TABLE). CTR mode is
symmetric, so ``ctr_crypt`` both encrypts and decrypts.
"""

from __future__ import annotations

import base64

from app.npvt.unlocker.npvt_tables import NR, SHIFT_ORDER, TY_BOXES, MBL, TBOXES_LAST, XOR_TABLE


def _shift_rows_like(b: list[int]) -> list[int]:
    return [b[SHIFT_ORDER[i]] for i in range(16)]


def core_transform(block: bytes | list[int]) -> list[int]:
    """One keystream block: whitebox AES with NR (=2) rounds."""
    buf = list(block)

    for _ in range(NR - 1):
        buf = _shift_rows_like(buf)

        for i11 in range(4):
            i13 = i11 * 4
            i14, i15, i16 = i13 + 1, i13 + 2, i13 + 3

            iC = TY_BOXES[i13][buf[i13]]
            iC2 = TY_BOXES[i14][buf[i14]]
            iC3 = TY_BOXES[i15][buf[i15]]
            iC4 = TY_BOXES[i16][buf[i16]]

            for i17 in range(4):
                i18 = (i11 * 24) + (i17 * 6)
                i19 = i17 * 8
                i20 = 28 - i19
                i22 = 24 - i19

                n1 = (iC >> i20) & 15
                n2 = (iC2 >> i20) & 15
                n3 = (iC3 >> i20) & 15
                n4 = (iC4 >> i20) & 15
                b10 = XOR_TABLE[i18][n1][n2]
                b11 = XOR_TABLE[i18 + 1][n3][n4]

                m1 = (iC >> i22) & 15
                m2 = (iC2 >> i22) & 15
                m3 = (iC3 >> i22) & 15
                m4 = (iC4 >> i22) & 15
                lo = XOR_TABLE[i18 + 5][XOR_TABLE[i18 + 2][m1][m2]][XOR_TABLE[i18 + 3][m3][m4]]
                hi = XOR_TABLE[i18 + 4][b10][b11]

                buf[i13 + i17] = lo | (hi << 4)

            iC5 = MBL[i13][buf[i13]]
            iC6 = MBL[i14][buf[i14]]
            iC7 = MBL[i15][buf[i15]]
            iC8 = MBL[i16][buf[i16]]

            for i27 in range(4):
                i28 = (i11 * 24) + (i27 * 6)
                i29 = i27 * 8
                i30 = 28 - i29
                i31 = 24 - i29

                n1 = (iC5 >> i30) & 15
                n2 = (iC6 >> i30) & 15
                n3 = (iC7 >> i30) & 15
                n4 = (iC8 >> i30) & 15
                a1 = XOR_TABLE[i28][n1][n2]
                a2 = XOR_TABLE[i28 + 1][n3][n4]
                hi = XOR_TABLE[i28 + 4][a1][a2]

                m1 = (iC5 >> i31) & 15
                m2 = (iC6 >> i31) & 15
                m3 = (iC7 >> i31) & 15
                m4 = (iC8 >> i31) & 15
                b1 = XOR_TABLE[i28 + 2][m1][m2]
                b2 = XOR_TABLE[i28 + 3][m3][m4]
                lo = XOR_TABLE[i28 + 5][b1][b2]

                buf[i13 + i27] = (hi << 4) | lo

    buf = _shift_rows_like(buf)
    for i in range(16):
        buf[i] = TBOXES_LAST[i][buf[i]]
    return buf


def _ctr_increment(counter: list[int]) -> None:
    i = 15
    while i > -1:
        counter[i] = (counter[i] + 1) & 0xFF
        if counter[i] != 0:
            break
        i -= 1


def ctr_crypt(nonce: bytes, data: bytes) -> bytes:
    """XOR ``data`` with the whitebox-AES-CTR keystream seeded at ``nonce``."""
    counter = list(nonce)
    out = bytearray(len(data))
    keystream: list[int] = [0] * 16
    for i in range(len(data)):
        if i % 16 == 0:
            keystream = core_transform(counter)
            _ctr_increment(counter)
        out[i] = keystream[i % 16] ^ data[i]
    return bytes(out)


# --- blob (nonce || ciphertext) encode/decode --------------------------------

def decrypt_blob(raw: bytes) -> bytes:
    """``raw`` = nonce[16] || ciphertext -> plaintext."""
    if len(raw) < 16:
        raise ValueError("blob too short to contain a 16-byte nonce")
    return ctr_crypt(raw[:16], raw[16:])


def encrypt_blob(nonce: bytes, plaintext: bytes) -> bytes:
    """plaintext -> nonce[16] || ciphertext."""
    if len(nonce) != 16:
        raise ValueError("nonce must be 16 bytes")
    return bytes(nonce) + ctr_crypt(nonce, plaintext)


def b64_decode_token(token: str) -> bytes:
    """Decode one comma-field, stripping the NPVT1 marker and whitespace."""
    token = token.replace("NPVT1", "")
    token = "".join(token.split())
    return base64.b64decode(token)


def b64_encode_blob(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")
