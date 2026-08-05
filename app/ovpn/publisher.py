"""Publish the .ovpn payload to GitHub under its own path.

Fully independent of :mod:`app.core.github_sync`:

* it writes only under ``<github_target_dir>/<github_subdir>/`` so the core
  subscription files (``active.txt`` …) are never rewritten;
* it keeps its **own** content hash in the private ``ovpn_settings`` table, so
  a change here never invalidates the core push state and vice versa — an ovpn
  update cannot trigger a commit for the main subscription, and a main-sub
  update cannot trigger one here;
* every failure is contained and reported, never raised into the worker loop.

The resulting subscription link is::

    https://raw.githubusercontent.com/<repo>/<branch>/[target_dir/]ovpn/index.txt
"""
from __future__ import annotations

import base64
import hashlib

import httpx

from app.core.logbook import get_logger
from app.core.settings_manager import settings
from app.ovpn.config import ovpn_settings

_API = "https://api.github.com"
_HASH_KEY = "push_hash"
log = get_logger()


def _gh() -> tuple[str, str, str, str]:
    token = settings.get("github_token") or ""
    repo = settings.get("github_repository") or ""
    branch = settings.get("github_branch") or "main"
    target_dir = (settings.get("github_target_dir") or "").strip("/")
    return token, repo, branch, target_dir


def base_path() -> str:
    _, _, _, target_dir = _gh()
    sub = ovpn_settings.github_subdir
    return f"{target_dir}/{sub}" if target_dir else sub


def raw_url(name: str) -> str:
    _, repo, branch, _ = _gh()
    if not repo:
        return ""
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{base_path()}/{name}"


def index_url() -> str:
    return raw_url(ovpn_settings.index_file)


def build_index(names: list[str]) -> str:
    """One raw URL per line — the consumable ovpn subscription index."""
    urls = [raw_url(n) for n in names]
    urls = [u for u in urls if u]
    return "\n".join(urls) + ("\n" if urls else "")


def _hash(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode())
        h.update(b"\0")
        h.update(files[name].encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


async def publish(files: dict[str, str]) -> dict:
    """Push ``files`` (relative to the ovpn sub-path). Never raises."""
    token, repo, branch, _ = _gh()
    if not token or not repo:
        return {"status": "skipped", "reason": "GitHub token/repository not configured"}
    if not files:
        return {"status": "skipped", "reason": "nothing to publish"}

    # No batch-level "unchanged" short-circuit here: the caller decides what to
    # push (blobs are pushed once, the index only when its own hash changes) and
    # _put_file already skips the commit when the remote blob is byte-identical.
    # A second guard at this level could silently swallow a legitimate re-push.
    content_hash = _hash(files)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    prefix = base_path()
    last_commit = ""
    pushed: list[str] = []
    try:
        async with httpx.AsyncClient(base_url=_API, headers=headers, timeout=30) as client:
            for name, content in files.items():
                last_commit = await _put_file(
                    client, repo, branch, f"{prefix}/{name}", content
                )
                pushed.append(name)
        await ovpn_settings.set_state(_HASH_KEY, content_hash)
        log.info("ovpn: pushed %d file(s) to %s, commit %s",
                 len(pushed), prefix, last_commit[:8])
        return {"status": "success", "commit": last_commit, "files": pushed}
    except httpx.HTTPError as exc:
        log.error("ovpn: GitHub push failed after %d file(s): %s", len(pushed), exc)
        return {"status": "failed", "reason": str(exc), "files": pushed}


async def _put_file(
    client: httpx.AsyncClient, repo: str, branch: str, path: str, content: str
) -> str:
    sha: str | None = None
    get = await client.get(f"/repos/{repo}/contents/{path}", params={"ref": branch})
    if get.status_code == 200:
        body = get.json()
        sha = body.get("sha")
        # Identical blob already on the remote — skip the commit entirely.
        remote = body.get("content") or ""
        if remote:
            try:
                if base64.b64decode(remote).decode("utf-8") == content:
                    return ""
            except (ValueError, UnicodeDecodeError):
                pass
    elif get.status_code != 404:
        get.raise_for_status()

    payload: dict = {
        "message": f"chore(ovpn): update {path.rsplit('/', 1)[-1]}",
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    put = await client.put(f"/repos/{repo}/contents/{path}", json=payload)
    put.raise_for_status()
    return put.json().get("commit", {}).get("sha", "")
