"""HTTP surface: auth guard, health probe and core read endpoints.

The app runs under FastAPI's TestClient, which drives the real lifespan — that
is what initialises the database on the same event loop the requests use. The
background work the lifespan would normally also start (the scheduler, the npvt
worker) is neutralised first: a test run must never contact Telegram, open TCP
connections to collected hosts, or push to GitHub.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import config
from app.core.security import hash_password
from app.database import _engine
from app.main import app


async def _anoop(*_args, **_kwargs) -> None:
    return None


@pytest.fixture(scope="module")
def client():
    from app.core.scheduler import scheduler
    # `app.npvt` re-exports the service *singleton* under this name, which is
    # exactly the object app.main resolves in its lifespan.
    from app.npvt import service as npvt_service

    saved = (scheduler.start, scheduler.stop, npvt_service.start, npvt_service.stop)
    scheduler.start = lambda: None
    scheduler.stop = _anoop
    npvt_service.start = _anoop
    npvt_service.stop = _anoop

    # Hand the TestClient's event loop a clean connection pool: pooled aiosqlite
    # connections are bound to the loop that created them, and the other test
    # modules run on their own loops.
    asyncio.run(_engine.dispose())
    try:
        with TestClient(app) as c:
            yield c
    finally:
        (scheduler.start, scheduler.stop, npvt_service.start, npvt_service.stop) = saved
        asyncio.run(_engine.dispose())


@pytest.fixture
def _no_auth():
    """Run with dashboard auth disabled (all three credentials blank)."""
    saved = (config.dashboard_user, config.dashboard_password, config.dashboard_password_hash)
    config.dashboard_user = ""
    config.dashboard_password = ""
    config.dashboard_password_hash = ""
    yield
    (config.dashboard_user, config.dashboard_password, config.dashboard_password_hash) = saved


@pytest.fixture
def _with_auth():
    """Run with HTTP Basic auth enabled using a hashed password."""
    saved = (config.dashboard_user, config.dashboard_password, config.dashboard_password_hash)
    config.dashboard_user = "admin"
    config.dashboard_password = ""
    config.dashboard_password_hash = hash_password("s3cret")
    yield
    (config.dashboard_user, config.dashboard_password, config.dashboard_password_hash) = saved


# ── health ───────────────────────────────────────────────────────────────────

def test_health_is_public_even_when_auth_is_enabled(client, _with_auth):
    # The container healthcheck has no credentials — this endpoint must stay open.
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ── auth guard ───────────────────────────────────────────────────────────────

def test_api_requires_credentials_when_auth_is_enabled(client, _with_auth):
    resp = client.get("/api/overview")
    assert resp.status_code == 401
    assert "Basic" in resp.headers.get("WWW-Authenticate", "")


def test_wrong_password_is_rejected(client, _with_auth):
    assert client.get("/api/overview", auth=("admin", "wrong")).status_code == 401


def test_wrong_username_is_rejected(client, _with_auth):
    assert client.get("/api/overview", auth=("nobody", "s3cret")).status_code == 401


def test_correct_credentials_are_accepted(client, _with_auth):
    assert client.get("/api/overview", auth=("admin", "s3cret")).status_code == 200


def test_dashboard_pages_are_also_guarded(client, _with_auth):
    assert client.get("/").status_code == 401


def test_auth_is_skipped_when_no_credentials_are_configured(client, _no_auth):
    assert client.get("/api/overview").status_code == 200


# ── read endpoints ───────────────────────────────────────────────────────────

def test_overview_reports_the_expected_shape(client, _no_auth):
    body = client.get("/api/overview").json()
    assert {"scheduler", "stats", "duplicates", "github", "telegram_configured"} <= set(body)
    assert {"active_count", "archive_count", "cooldown_count"} <= set(body["stats"])


def test_channels_list_is_json(client, _no_auth):
    assert isinstance(client.get("/api/channels").json(), list)


def test_settings_never_expose_the_github_token(client, _no_auth):
    body = client.get("/api/settings").json()
    assert "github_token" in body
    # Either blank or masked — the real value must never leave the server.
    assert body["github_token"] == "" or set(body["github_token"][:8]) == {"*"}


def test_blacklist_returns_all_three_kinds(client, _no_auth):
    body = client.get("/api/blacklist").json()
    assert set(body) == {"domains", "ips", "keywords"}


def test_unknown_dashboard_page_is_404(client, _no_auth):
    assert client.get("/definitely-not-a-page").status_code == 404


def test_known_dashboard_page_renders(client, _no_auth):
    resp = client.get("/overview")
    assert resp.status_code == 200
    assert "v2get" in resp.text


def test_adding_a_channel_rejects_an_empty_username(client, _no_auth):
    assert client.post("/api/channels", json={"username": "  @  "}).status_code in (400, 422)


def test_blacklist_rejects_an_unknown_kind(client, _no_auth):
    resp = client.post("/api/blacklist/add", json={"kind": "bogus", "entry": "x"})
    assert resp.status_code == 422
