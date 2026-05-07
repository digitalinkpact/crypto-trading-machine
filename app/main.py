"""FastAPI entrypoint. Wires routes + APScheduler lifespan."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router
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
app.include_router(router)
