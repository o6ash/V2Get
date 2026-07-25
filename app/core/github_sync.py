"""Publish subscription files to GitHub via the Contents API.

Pushes only when the aggregate content changes — a content hash of all
published files is compared against the last successful push and unchanged
runs skip the commit entirely.
"""
from __future__ import annotations

import base64
import hashlib

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logbook import get_logger
from app.core.settings_manager import settings
from app.models import GithubState, utcnow

_API = "https://api.github.com"
log = get_logger()


async def _get_state(session: AsyncSession) -> GithubState:
    state = await session.get(GithubState, 1)
    if not state:
        state = GithubState(id=1)
        session.add(state)
        await session.flush()
    return state


def _aggregate_hash(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode())
        h.update(b"\0")
        h.update(files[name].encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


async def publish(session: AsyncSession, files: dict[str, str]) -> dict:
    """Push ``files`` to the configured repo. Returns a status dict."""
    state = await _get_state(session)
    token = settings.get("github_token")
    repo = settings.get("github_repository")
    branch = settings.get("github_branch") or "main"
    target_dir = (settings.get("github_target_dir") or "").strip("/")

    if not token or not repo:
        state.last_status = "unconfigured"
        return {"status": "skipped", "reason": "GitHub token/repository not configured"}

    content_hash = _aggregate_hash(files)
    if content_hash == state.last_content_hash:
        log.info("GitHub: no changes detected — skipping commit/push")
        state.last_status = "skipped"
        return {"status": "skipped", "reason": "no changes detected"}

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    last_commit = ""
    try:
        async with httpx.AsyncClient(base_url=_API, headers=headers, timeout=30) as client:
            for name, content in files.items():
                path = f"{target_dir}/{name}" if target_dir else name
                last_commit = await _put_file(client, repo, branch, path, content)
        state.last_status = "success"
        state.last_commit = last_commit
        state.last_push_at = utcnow()
        state.last_content_hash = content_hash
        log.info("GitHub: pushed %d file(s), commit %s", len(files), last_commit[:8])
        return {"status": "success", "commit": last_commit, "files": list(files)}
    except httpx.HTTPError as exc:
        log.error("GitHub push failed: %s", exc)
        state.last_status = "failed"
        return {"status": "failed", "reason": str(exc)}


async def _put_file(
    client: httpx.AsyncClient, repo: str, branch: str, path: str, content: str
) -> str:
    # Look up the existing blob sha (required for updates).
    sha: str | None = None
    get = await client.get(f"/repos/{repo}/contents/{path}", params={"ref": branch})
    if get.status_code == 200:
        sha = get.json().get("sha")
    elif get.status_code not in (404,):
        get.raise_for_status()

    payload: dict = {
        "message": f"chore: update {path}",
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha

    put = await client.put(f"/repos/{repo}/contents/{path}", json=payload)
    put.raise_for_status()
    return put.json().get("commit", {}).get("sha", "")


async def get_status(session: AsyncSession) -> dict:
    state = (await session.execute(select(GithubState))).scalar_one_or_none()
    repo = settings.get("github_repository")
    target_dir = (settings.get("github_target_dir") or "").strip("/")
    branch = settings.get("github_branch") or "main"

    def raw_url(name: str) -> str:
        if not repo:
            return ""
        path = f"{target_dir}/{name}" if target_dir else name
        return f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"

    if not state:
        return {"last_status": "never", "last_commit": "", "last_push_at": None,
                "repository": repo, "raw_base": raw_url("")}
    return {
        "last_status": state.last_status,
        "last_commit": state.last_commit,
        "last_push_at": state.last_push_at.isoformat() if state.last_push_at else None,
        "repository": repo,
        "configured": bool(settings.get("github_token") and repo),
        "raw_base": raw_url(""),
    }
