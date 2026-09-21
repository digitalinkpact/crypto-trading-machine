"""FastAPI entrypoint. Wires routes + APScheduler lifespan."""
from __future__ import annotations

import base64
import binascii
import hmac
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import router
from app.config import get_settings
from app.exchange.filters import filters
from app.logging_setup import configure_logging, get_logger
from app.scheduler import build_scheduler
from app.trading.paper import paper_exchange

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    # Load Binance.US filters (public endpoint, no auth needed).
    try:
        await filters.load()
    except Exception as exc:  # noqa: BLE001
        log.warning("filter preload failed: %s", exc)
    # Seed paper account on first run.
    paper_exchange.ensure_seeded()
    scheduler = build_scheduler()
    scheduler.start()
    log.info("scheduler started; jobs=%s", [j.id for j in scheduler.get_jobs()])
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        log.info("scheduler stopped")


app = FastAPI(title="AI Crypto Trading Machine", lifespan=lifespan)


def _auth_enabled() -> bool:
    s = get_settings()
    return bool(s.app_basic_auth_user and s.app_basic_auth_password.get_secret_value())


def _check_basic_auth(header_value: str) -> bool:
    if not header_value.lower().startswith("basic "):
        return False
    token = header_value.split(" ", 1)[1].strip()
    try:
        raw = base64.b64decode(token).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return False
    if ":" not in raw:
        return False
    user, password = raw.split(":", 1)
    s = get_settings()
    exp_user = s.app_basic_auth_user
    exp_pass = s.app_basic_auth_password.get_secret_value()
    return hmac.compare_digest(user, exp_user) and hmac.compare_digest(password, exp_pass)


@app.middleware("http")
async def basic_auth_guard(request: Request, call_next):
    if not _auth_enabled() or request.url.path == "/healthz":
        return await call_next(request)

    auth = request.headers.get("authorization", "")
    if _check_basic_auth(auth):
        return await call_next(request)

    return JSONResponse(
        status_code=401,
        content={"detail": "Unauthorized"},
        headers={"WWW-Authenticate": "Basic realm=crypto-bot"},
    )


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(router)
