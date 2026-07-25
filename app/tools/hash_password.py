"""Generate a DASHBOARD_PASSWORD_HASH value for the dashboard login.

Usage:
    python -m app.tools.hash_password            # prompts (hidden input)
    python -m app.tools.hash_password 'secret'   # non-interactive

Copy the printed line into .env and leave DASHBOARD_PASSWORD blank.
"""
from __future__ import annotations

import getpass
import sys

from app.core.security import hash_password


def main() -> None:
    if len(sys.argv) > 1:
        password = sys.argv[1]
    else:
        password = getpass.getpass("Dashboard password: ")
        if password != getpass.getpass("Confirm password: "):
            print("Passwords do not match.", file=sys.stderr)
            raise SystemExit(1)
    if not password:
        print("Password must not be empty.", file=sys.stderr)
        raise SystemExit(1)
    print("DASHBOARD_PASSWORD_HASH=" + hash_password(password))


if __name__ == "__main__":
    main()
