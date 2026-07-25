"""Application entrypoint: wires lifespan, API, dashboard and optional auth."""
from __future__ import annotations

import hashlib
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.responses import Response

from app import __version__
from app.api.routes import router as api_router
from app.config import config
from app.core.blacklist import blacklist
from app.core.logbook import get_logger, setup_logging
from app.core.scheduler import scheduler
from app.core.security import verify_password
from app.core.settings_manager import settings
from app.database import init_db

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "dashboard" / "static"
TEMPLATES = Jinja2Templates(directory=str(BASE_DIR / "dashboard" / "templates"))

_security = HTTPBasic(auto_error=False)


def _asset_version() -> str:
    """Short content hash of the static assets, used to bust browser caches.

    The script/style URLs embed this, so a redeploy that changes app.js or
    styles.css yields a new URL and clients always fetch the current asset
    instead of a stale cached copy.
    """
    h = hashlib.sha256()
    for name in ("app.js", "styles.css"):
        p = STATIC_DIR / name
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()[:12]


ASSET_VERSION = _asset_version()


class _NoCacheStatic(StaticFiles):
    """Serve static files with revalidation so stale assets are never used.

    ETags still make this cheap (304 Not Modified when unchanged); combined with
    the versioned URLs above, clients reliably pick up new frontend code.
    """

    def file_response(self, *args, **kwargs) -> Response:
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    log = get_logger()
    log.info("Starting v2get %s", __version__)
    config.ensure_dirs()
    await init_db()
    await settings.load()
    blacklist.ensure_files()
    # Isolated .npvt pipeline — self-contained; failures here never block the
    # core platform (see app.npvt). Started *before* the scheduler so its one-off
    # table creation / settings load never contends with a collection run's
    # long-lived write transaction (SQLite serialises writers).
    from app.npvt import service as npvt_service
    await npvt_service.start()
    scheduler.start()
    log.info("Startup complete — dashboard on http://localhost:8080")
    try:
        yield
    finally:
        from app.npvt import service as npvt_service
        await npvt_service.stop()
        await scheduler.stop()
        from app.core.telegram_client import collector_client
        await collector_client.close()
        log.info("Shutdown complete")


def _check_auth(credentials: HTTPBasicCredentials | None = Depends(_security)) -> None:
    user = config.dashboard_user
    pwd = config.dashboard_password
    pwd_hash = config.dashboard_password_hash
    if not user and not pwd and not pwd_hash:
        return  # auth disabled
    if credentials is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    ok_user = secrets.compare_digest(credentials.username, user)
    # The password is never compared in plaintext when a hash is configured;
    # verify_password runs PBKDF2 and compares the digests in constant time.
    if pwd_hash:
        ok_pwd = verify_password(credentials.password, pwd_hash)
    else:
        ok_pwd = secrets.compare_digest(credentials.password, pwd)
    if not (ok_user and ok_pwd):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


app = FastAPI(title="v2get", version=__version__, lifespan=lifespan)


@app.get("/api/health")
async def health() -> dict:
    """Unauthenticated liveness probe (used by the Docker healthcheck)."""
    return {"status": "ok"}


app.include_router(api_router, dependencies=[Depends(_check_auth)])
# Isolated .npvt module router (own prefix /api/npvt); shares the auth guard.
from app.npvt import router as npvt_router  # noqa: E402

app.include_router(npvt_router, prefix="/api", dependencies=[Depends(_check_auth)])
app.mount("/static", _NoCacheStatic(directory=str(STATIC_DIR)), name="static")

PAGES = {
    "overview": "Overview",
    "channels": "Channels",
    "active": "Active Configs",
    "archive": "Archive",
    "cooldown": "Cooldown",
    "blacklist": "Blacklist",
    "npvt": "NPVT",
    "logs": "Logs",
    "settings": "Settings",
}


@app.get("/", response_class=HTMLResponse)
@app.get("/{page}", response_class=HTMLResponse)
async def dashboard(request: Request, page: str = "overview", _: None = Depends(_check_auth)) -> HTMLResponse:
    if page not in PAGES:
        raise HTTPException(404, "Unknown page")
    return TEMPLATES.TemplateResponse(
        "index.html",
        {"request": request, "page": page, "pages": PAGES, "version": __version__,
         "asset_v": ASSET_VERSION},
    )
