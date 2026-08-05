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


def links_url() -> str:
    return raw_url(ovpn_settings.links_file)


def published_name(stored: str) -> str:
    """Remote file name for a locally stored profile.

    Profiles are published with a ``.txt`` extension: GitHub serves ``.ovpn``
    as an opaque download, while ``.txt`` renders inline, so the raw link can
    be read (and pasted) directly instead of being fetched as a binary blob.
    """
    return stored[:-5] + ".txt" if stored.lower().endswith(".ovpn") else stored


def build_links(names: list[str]) -> str:
    """One raw URL per line — for clients that import a profile *by URL*."""
    urls = [raw_url(published_name(n)) for n in names]
    urls = [u for u in urls if u]
    return "\n".join(urls) + ("\n" if urls else "")


BEGIN = "# ===== BEGIN {name} ====="
END = "# ===== END {name} ====="


def build_index(entries: list[tuple[str, str]], budget: int) -> tuple[str, list[str]]:
    """The subscription itself: the *contents* of every profile, concatenated.

    ``entries`` is ``[(stored_name, profile_text), …]`` newest-first. Each block
    is delimited by ``#`` comment markers — a comment in OpenVPN's own syntax,
    so a block copied out of the bundle is a valid profile as-is.

    Returns the bundle text and the names actually included; profiles are added
    newest-first until ``budget`` bytes are reached (GitHub's contents API gets
    unreliable around 1 MB, so the whole set is not always publishable).
    """
    parts: list[str] = []
    included: list[str] = []
    size = 0
    for name, text in entries:
        stem = name[:-5] if name.lower().endswith(".ovpn") else name
        body = text.strip("\n")
        block = f"{BEGIN.format(name=stem)}\n{body}\n{END.format(name=stem)}\n"
        if size + len(block.encode("utf-8")) > budget and included:
            break
        parts.append(block)
        included.append(name)
        size += len(block.encode("utf-8"))
    return "\n".join(parts), included


def _hash(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode())
        h.update(b"\0")
        h.update(files[name].encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def blob_sha(content: str) -> str:
    """Git's own object id for a blob — computable locally, no API call.

    Comparing this against the sha GitHub reports for a path tells us whether a
    push would be a no-op, without ever downloading the remote content. That
    matters because the contents API refuses to return files over 1 MB, and the
    inlined subscription bundle grows past that.
    """
    data = content.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324


async def _remote_shas(
    client: httpx.AsyncClient, repo: str, branch: str, prefix: str
) -> dict[str, str]:
    """``{file name: blob sha}`` for the ovpn folder — one request, any size."""
    res = await client.get(f"/repos/{repo}/contents/{prefix}", params={"ref": branch})
    if res.status_code == 404:
        return {}
    res.raise_for_status()
    body = res.json()
    if not isinstance(body, list):
        return {}
    return {e["name"]: e["sha"] for e in body if e.get("type") == "file"}


async def publish(files: dict[str, str]) -> dict:
    """Push ``files`` (relative to the ovpn sub-path). Never raises."""
    token, repo, branch, _ = _gh()
    if not token or not repo:
        return {"status": "skipped", "reason": "GitHub token/repository not configured"}
    if not files:
        return {"status": "skipped", "reason": "nothing to publish"}

    # No batch-level "unchanged" short-circuit here: the caller decides what to
    # push (blobs are pushed once, the index only when its own hash changes) and
    # identical blobs are skipped per file below. A second guard at this level
    # could silently swallow a legitimate re-push.
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
            remote = await _remote_shas(client, repo, branch, prefix)
            for name, content in files.items():
                sha = remote.get(name)
                # Byte-identical blob already on the remote — no commit needed,
                # but the file still counts as published for the caller.
                if sha and sha == blob_sha(content):
                    pushed.append(name)
                    continue
                last_commit = await _put_file(
                    client, repo, branch, f"{prefix}/{name}", content, sha
                )
                pushed.append(name)
        await ovpn_settings.set_state(_HASH_KEY, content_hash)
        log.info("ovpn: pushed %d file(s) to %s, commit %s",
                 len(pushed), prefix, last_commit[:8])
        return {"status": "success", "commit": last_commit, "files": pushed}
    except httpx.HTTPError as exc:
        log.error("ovpn: GitHub push failed after %d file(s): %s", len(pushed), exc)
        return {"status": "failed", "reason": str(exc), "files": pushed}


async def delete_files(names: list[str]) -> dict:
    """Remove blobs from the remote ovpn/ folder. Never raises."""
    token, repo, branch, _ = _gh()
    if not token or not repo or not names:
        return {"status": "skipped", "reason": "nothing to delete"}
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    prefix = base_path()
    removed: list[str] = []
    try:
        async with httpx.AsyncClient(base_url=_API, headers=headers, timeout=30) as client:
            for name in names:
                path = f"{prefix}/{name}"
                get = await client.get(f"/repos/{repo}/contents/{path}", params={"ref": branch})
                if get.status_code == 404:
                    continue
                get.raise_for_status()
                sha = get.json().get("sha")
                if not sha:
                    continue
                res = await client.request(
                    "DELETE",
                    f"/repos/{repo}/contents/{path}",
                    json={"message": f"chore(ovpn): drop {name}", "sha": sha, "branch": branch},
                )
                res.raise_for_status()
                removed.append(name)
        log.info("ovpn: deleted %d stale blob(s) from %s", len(removed), prefix)
        return {"status": "success", "files": removed}
    except httpx.HTTPError as exc:
        log.error("ovpn: delete failed after %d file(s): %s", len(removed), exc)
        return {"status": "failed", "reason": str(exc), "files": removed}


async def _put_file(
    client: httpx.AsyncClient,
    repo: str,
    branch: str,
    path: str,
    content: str,
    sha: str | None = None,
) -> str:
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
