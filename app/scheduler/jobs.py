"""Scheduler wiring. Single AsyncIOScheduler shared by the FastAPI app."""
from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import SYMBOLS, TIMEFRAMES
from app.data import OHLCVRepository
from app.logging_setup import get_logger
from app.trading import autopilot

log = get_logger(__name__)


async def refresh_market_data() -> None:
    repo = OHLCVRepository()
    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            try:
                await repo.get(symbol, tf, refresh=True)
            except Exception as exc:  # noqa: BLE001
                log.warning("refresh failed %s/%s: %s", symbol, tf.value, exc)


async def autopilot_tick() -> None:
    """Run agents and execute signals only when the user has hit Start."""
    await autopilot.tick()


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(refresh_market_data, CronTrigger(minute="*/15"), id="market_data")
    scheduler.add_job(autopilot_tick, CronTrigger(minute="2,17,32,47"), id="autopilot")
    return scheduler

