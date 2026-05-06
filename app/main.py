"""FastAPI entrypoint. Wires routes + APScheduler lifespan."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router
from app.logging_setup import configure_logging, get_logger
from app.scheduler import build_scheduler

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
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
