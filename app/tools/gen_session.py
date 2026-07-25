"""Interactive helper to mint a Telethon StringSession.

Run once on your workstation (not in the container):

    pip install telethon
    python -m app.tools.gen_session

It will prompt for your api_id, api_hash, phone number and the login code, then
print a StringSession value to paste into TELEGRAM_SESSION in your .env.
"""
from __future__ import annotations

from telethon import TelegramClient
from telethon.sessions import StringSession


def main() -> None:
    api_id = int(input("api_id: ").strip())
    api_hash = input("api_hash: ").strip()
    with TelegramClient(StringSession(), api_id, api_hash) as client:
        # `with` triggers the interactive login (phone + code [+ 2FA password]).
        print("\n── Your StringSession (keep it secret) ──")
        print(client.session.save())
        print("\nPaste it into .env as TELEGRAM_SESSION=…")


if __name__ == "__main__":
    main()
