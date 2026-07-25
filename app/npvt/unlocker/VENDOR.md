# Vendored NPVT toolkit

`npvt.py`, `npvt_crypto.py` and `npvt_tables.py` are vendored from the
standalone **npvt-bot** toolkit and are the local replacement for the external
Telegram bot that previously unlocked `.npvt` files.

## Provenance

- Upstream: `npvt-bot` (`src/npvt.py`, `src/npvt_crypto.py`, `src/npvt_tables.py`)
- The cipher tables in `npvt_tables.py` are auto-generated from the open-source
  [Pantegnos](https://github.com/FrontierTM/Pantegnos) reference implementation
  (`tools/dump_test.go` in the upstream toolkit). **Do not hand-edit them.**
- Upstream's `src/bot.py` (a `python-telegram-bot` front-end) is deliberately
  **not** vendored — v2get calls the format ops directly.

## Local modifications

Only the imports were rewritten from flat (`from npvt_crypto import ...`) to
package-absolute (`from app.npvt.unlocker.npvt_crypto import ...`) so the
modules import cleanly inside the app package. The logic is untouched, which
keeps re-syncing with upstream a two-line `sed`.

## Dependencies

None. The modules use only the standard library (`base64`, `json`, `re`,
`urllib.parse`, `dataclasses`) — no new entry in `requirements.txt`.

## Format notes

A `.npvt` file is `NPVT1\n` followed by comma-separated base64 blobs, each
`base64(nonce[16] || ciphertext)` under whitebox AES-CTR. Decrypted, the blobs
are a profile count, the profiles JSON array, and a top-level lock object.
Unlocking neutralises `isLocked`, `blockRootedAndJailbroken` and `message`, then
re-encrypts with the original nonce (CTR is symmetric, so one routine does both
directions).

Newer container versions (`npv3` / `npv4`, different framing) are **not**
supported and raise `UnlockError`.

## Verification

Round-trip fidelity is asserted in `tests/test_npvt_unlocker.py` against the
fixtures in `tests/fixtures/`: unlocking `locked_sample.npvt` reproduces
`unlocked_sample.npvt` byte-for-byte, and every exported URI is accepted by
v2get's own `app.core.parser`.
