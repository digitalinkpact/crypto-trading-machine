"""FastAPI entrypoint. Wires routes + APScheduler lifespan."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router
from app.auth import auth_guard, auth_router
from app.exchange.filters import filters
from app.logging_setup import configure_logging, get_logger
from app.scheduler import build_scheduler
from app.storage import storage
from app.trading.paper import paper_exchange

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    # Drop expired sessions/tokens on boot.
    try:
        storage.purge_expired_sessions()
    except Exception as exc:  # noqa: BLE001
        log.warning("purge expired sessions failed: %s", exc)
    # Load Binance.US filters (public endpoint, no auth needed).
    try:
        await filters.load()
    except Exception as exc:  # noqa: BLE001
        log.warning("filter preload failed: %s", exc)
    # Seed paper account on first run.
    try:
        paper_exchange.ensure_seeded()
    except Exception as exc:  # noqa: BLE001
        log.exception("paper account seed failed: %s", exc)

    scheduler = None
    try:
        scheduler = build_scheduler()
        scheduler.start()
        log.info("scheduler started; jobs=%s", [j.id for j in scheduler.get_jobs()])
    except Exception as exc:  # noqa: BLE001
        log.exception("scheduler start failed: %s", exc)

    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
            log.info("scheduler stopped")


app = FastAPI(title="AI Crypto Trading Machine", lifespan=lifespan)

# Session-based auth guard. Replaces the previous HTTP Basic middleware.
app.middleware("http")(auth_guard)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(auth_router)
app.include_router(router)
