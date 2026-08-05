"""One-shot migration: drop the stale ovpn/*.ovpn blobs and reset publish state.

Profiles are now published as ``.txt`` (readable raw links) and the index file
carries the profile *contents*, so the old ``.ovpn`` blobs and the recorded
publish hashes are obsolete. Paced deletes keep GitHub's secondary rate limit
(content-creating requests per minute) happy.
"""
from __future__ import annotations

import asyncio

from app.database import SessionLocal, init_db
from app.ovpn import publisher
from app.ovpn.config import ovpn_settings
from app.ovpn.models import OvpnFile
from sqlalchemy import select, update


async def main() -> None:
    await init_db()
    await ovpn_settings.load()

    token, repo, branch, _ = publisher._gh()
    prefix = publisher.base_path()
    import httpx

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with httpx.AsyncClient(base_url="https://api.github.com",
                                 headers=headers, timeout=30) as client:
        remote = await publisher._remote_shas(client, repo, branch, prefix)
    stale = sorted(n for n in remote if n.lower().endswith(".ovpn"))
    print(f"remote files: {len(remote)} | stale .ovpn: {len(stale)}")

    removed = 0
    for i in range(0, len(stale), 20):
        chunk = stale[i:i + 20]
        res = await publisher.delete_files(chunk)
        removed += len(res.get("files") or [])
        print(f"  deleted {removed}/{len(stale)} ({res.get('status')})")
        await asyncio.sleep(5)

    async with SessionLocal() as session:
        await session.execute(update(OvpnFile).values(published=False))
        await session.commit()
        total = len((await session.execute(select(OvpnFile.id))).scalars().all())
    for key in ("index_hash", "links_hash", "push_hash"):
        await ovpn_settings.set_state(key, "")
    print(f"reset published=0 on {total} row(s); cleared publish hashes")


if __name__ == "__main__":
    asyncio.run(main())
