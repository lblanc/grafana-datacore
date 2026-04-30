"""
FastAPI app exposing the setup UI.

Routes:
  GET  /login        -> login form
  POST /login        -> set session cookie
  POST /logout       -> clear cookie
  GET  /             -> main settings page (requires auth)
  POST /save         -> persist settings
  POST /test         -> connectivity tests (returns JSON)
  POST /reload       -> SIGHUP the collector container
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Form,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer

import json
from datetime import datetime, timezone

from fastapi.responses import PlainTextResponse, StreamingResponse

from .docker_logs import stream_logs, tail_logs
from .docker_reload import reload_collector
from .health_checks import test_datacore, test_influx
from .settings_store import (
    PERFORMANCE_CATEGORIES,
    CategoryConfig,
    Settings,
    load_settings,
    save_settings,
)


LOGGER = logging.getLogger("setup_ui")

# Configuration paths inside the container
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
INI_PATH = CONFIG_DIR / "collector.ini"
ENV_PATH = CONFIG_DIR / ".env"
STATUS_PATH = Path(os.environ.get("COLLECTOR_STATUS_FILE", "/status/status.json"))

# Auth
ADMIN_USER = os.environ.get("SETUP_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("SETUP_ADMIN_PASSWORD", "admin")
SECRET_KEY = os.environ.get("SETUP_SECRET_KEY") or secrets.token_urlsafe(32)
COOKIE_NAME = "setup_session"
COOKIE_MAX_AGE = 3600 * 8  # 8h

_serializer = URLSafeSerializer(SECRET_KEY, salt="setup-session")

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="DataCore Collector Setup", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# ---------------------------------------------------------------- auth
def _make_session_token() -> str:
    return _serializer.dumps({"user": ADMIN_USER})


def _check_session(token: Optional[str]) -> bool:
    if not token:
        return False
    try:
        data = _serializer.loads(token)
    except BadSignature:
        return False
    return data.get("user") == ADMIN_USER


def require_auth(session: Optional[str] = Cookie(default=None, alias=COOKIE_NAME)):
    if not _check_session(session):
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return True


# ---------------------------------------------------------------- pages
@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: Optional[str] = None):
    return templates.TemplateResponse(
        request, "login.html", {"error": error}
    )


@app.post("/login")
def login_submit(
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
):
    if not (
        secrets.compare_digest(username, ADMIN_USER)
        and secrets.compare_digest(password, ADMIN_PASSWORD)
    ):
        return RedirectResponse(
            url="/login?error=Invalid+credentials", status_code=303
        )
    redirect = RedirectResponse(url="/", status_code=303)
    redirect.set_cookie(
        COOKIE_NAME,
        _make_session_token(),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return redirect


@app.post("/logout")
def logout():
    redirect = RedirectResponse(url="/login", status_code=303)
    redirect.delete_cookie(COOKIE_NAME)
    return redirect


@app.get("/", response_class=HTMLResponse)
def index(request: Request, _auth: bool = Depends(require_auth)):
    settings = load_settings(INI_PATH)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "s": settings,
            "categories": PERFORMANCE_CATEGORIES,
            "saved": request.query_params.get("saved") == "1",
            "reloaded": request.query_params.get("reloaded") == "1",
        },
    )


# ---------------------------------------------------------------- save
def _bool_form(value: Optional[str]) -> bool:
    return value in ("on", "true", "1", "yes")


@app.post("/save")
async def save(request: Request, _auth: bool = Depends(require_auth)):
    form = await request.form()

    settings = Settings(
        dcs_rest_host=form.get("dcs_rest_host", "").strip(),
        dcs_server_host=form.get("dcs_server_host", "").strip(),
        dcs_username=form.get("dcs_username", "").strip(),
        dcs_password=form.get("dcs_password", ""),
        dcs_scheme=form.get("dcs_scheme", "https"),
        dcs_verify_tls=_bool_form(form.get("dcs_verify_tls")),
        dcs_api_version=form.get("dcs_api_version", "1.0").strip(),
        influx_url=form.get("influx_url", "").strip(),
        influx_db=form.get("influx_db", "").strip(),
        influx_user=form.get("influx_user", "").strip(),
        influx_password=form.get("influx_password", ""),
        influx_create_db=_bool_form(form.get("influx_create_db")),
        interval_seconds=int(form.get("interval_seconds", "30") or 30),
        log_level=form.get("log_level", "INFO"),
    )

    for cat in PERFORMANCE_CATEGORIES:
        settings.categories[cat] = CategoryConfig(
            enabled=_bool_form(form.get(f"cat_{cat}_enabled")),
            include_names=form.get(f"cat_{cat}_include_names", "").strip(),
            exclude_names=form.get(f"cat_{cat}_exclude_names", "").strip(),
            include_counters=form.get(f"cat_{cat}_include_counters", "").strip(),
            exclude_counters=form.get(f"cat_{cat}_exclude_counters", "").strip(),
        )

    save_settings(settings, INI_PATH, ENV_PATH)
    LOGGER.info("Settings saved by admin (%d categories)", len(settings.categories))

    if _bool_form(form.get("reload_after_save")):
        ok, msg = reload_collector()
        target = "/?saved=1&reloaded=1" if ok else f"/?saved=1&reload_error={msg}"
        return RedirectResponse(url=target, status_code=303)

    return RedirectResponse(url="/?saved=1", status_code=303)


# ---------------------------------------------------------------- test
@app.post("/test")
async def test_endpoint(request: Request, _auth: bool = Depends(require_auth)):
    """Run connectivity tests against the values currently in the form
    (so the user can validate before saving). Returns JSON."""
    form = await request.form()
    s = Settings(
        dcs_rest_host=form.get("dcs_rest_host", "").strip(),
        dcs_server_host=form.get("dcs_server_host", "").strip(),
        dcs_username=form.get("dcs_username", "").strip(),
        dcs_password=form.get("dcs_password", ""),
        dcs_scheme=form.get("dcs_scheme", "https"),
        dcs_verify_tls=_bool_form(form.get("dcs_verify_tls")),
        dcs_api_version=form.get("dcs_api_version", "1.0").strip(),
        influx_url=form.get("influx_url", "").strip(),
        influx_db=form.get("influx_db", "").strip(),
        influx_user=form.get("influx_user", "").strip(),
        influx_password=form.get("influx_password", ""),
    )

    target = form.get("target", "all")
    results = {}
    if target in ("all", "datacore"):
        ok, msg = test_datacore(s)
        results["datacore"] = {"ok": ok, "message": msg}
    if target in ("all", "influx"):
        ok, msg = test_influx(s)
        results["influx"] = {"ok": ok, "message": msg}
    return JSONResponse(results)


# ---------------------------------------------------------------- reload
@app.post("/reload")
def reload_endpoint(_auth: bool = Depends(require_auth)):
    ok, msg = reload_collector()
    return JSONResponse({"ok": ok, "message": msg})


# ---------------------------------------------------------------- status
def _humanize_age(iso: Optional[str]) -> Optional[str]:
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return None
    delta = (datetime.now(timezone.utc) - ts).total_seconds()
    if delta < 0:
        return f"in {abs(int(delta))}s"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta / 60)}m ago"
    return f"{int(delta / 3600)}h ago"


@app.get("/status")
def status_endpoint(_auth: bool = Depends(require_auth)):
    if not STATUS_PATH.exists():
        return JSONResponse(
            {
                "available": False,
                "message": (
                    "Status file not found. The collector container may "
                    "not be running yet, or the shared volume is missing."
                ),
            }
        )
    try:
        payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return JSONResponse(
            {"available": False, "message": f"Could not read status: {exc}"}
        )
    payload["available"] = True
    payload["updated_age"] = _humanize_age(payload.get("updated_at"))
    payload["next_cycle_age"] = _humanize_age(payload.get("next_cycle_at"))
    return JSONResponse(payload)


# ---------------------------------------------------------------- logs
@app.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, _auth: bool = Depends(require_auth)):
    return templates.TemplateResponse(request, "logs.html", {})


@app.get("/logs/tail", response_class=PlainTextResponse)
def logs_tail(
    n: int = 200,
    _auth: bool = Depends(require_auth),
):
    n = max(10, min(n, 5000))
    ok, content = tail_logs(tail=n)
    if not ok:
        return PlainTextResponse(content, status_code=502)
    return PlainTextResponse(content)


@app.get("/logs/stream")
def logs_stream(
    tail: int = 50,
    _auth: bool = Depends(require_auth),
):
    """Server-Sent Events stream of collector logs."""
    tail = max(10, min(tail, 1000))

    def event_source():
        # Initial comment to flush headers immediately
        yield ": connected\n\n"
        for line in stream_logs(tail=tail):
            # Strip ANSI / trailing whitespace; SSE requires no bare newlines
            clean = line.rstrip("\n")
            for piece in clean.split("\n"):
                yield f"data: {piece}\n\n"

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable proxy buffering if any
        },
    )
