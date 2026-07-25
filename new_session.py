"""Interactive: mint a fresh Telethon StringSession and write it into .env.

Run this yourself in a real terminal (it asks for the Telegram login code):

    cd ~/projects/v2get
    .venv/bin/python new_session.py

api_id / api_hash are read from .env, so you only enter the phone number, the
code Telegram sends you, and your 2FA password if you have one. On success the
new session is written straight into .env (mode 600) - it is never printed, so
it can't end up in a chat log or your shell history.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ENV = Path(__file__).parent / ".env"


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV.read_text(encoding="utf-8").split("\n"):
        if line.strip().startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip()
    return values


def write_env_key(key: str, value: str) -> None:
    lines = ENV.read_text(encoding="utf-8").split("\n")
    pattern = re.compile(rf"^\s*#?\s*{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV.write_text("\n".join(lines), encoding="utf-8")
    os.chmod(ENV, 0o600)


def main() -> int:
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError:
        print("telethon is missing - run: .venv/bin/pip install -r requirements.txt")
        return 1

    env = read_env()
    api_id_raw = env.get("TELEGRAM_API_ID", "")
    api_hash = env.get("TELEGRAM_API_HASH", "")

    if not api_id_raw or not api_hash:
        print("TELEGRAM_API_ID / TELEGRAM_API_HASH are not set in .env.")
        print("Get them from https://my.telegram.org -> API development tools.")
        return 1

    print(f"Using api_id {api_id_raw} from .env")
    print("You will be asked for your phone number, then the login code.\n")

    with TelegramClient(StringSession(), int(api_id_raw), api_hash) as client:
        me = client.get_me()
        session = client.session.save()

    write_env_key("TELEGRAM_SESSION", session)
    who = getattr(me, "username", None) or getattr(me, "first_name", "?")
    print(f"\nLogged in as: {who} (id {getattr(me, 'id', '?')})")
    print(f"New session written to {ENV} (mode 600). It was not printed.")
    print("\nNow restart v2get so it picks the new session up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
