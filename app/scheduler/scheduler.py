"""ProfitStream scheduler profile.

Prioritizes frequent execution checks (1m) while keeping heavyweight jobs on
slower cadences to avoid overloading live trading.
"""
from __future__ import annotations

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.scheduler.jobs import (
    autopilot_tick,
    equity_snapshot,
    llm_signal_pass,
    ml_learning_pass,
    reconcile_portfolio,
    refresh_market_data,
)


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    # Strategy uses 1m/5m/15m/1h bars; refresh cache on 5m cadence.
    scheduler.add_job(refresh_market_data, CronTrigger(minute="*/5"), id="market_data")
    # 1m execution cadence with hard safety/risk gates in autopilot.
    scheduler.add_job(autopilot_tick, CronTrigger(minute="*"), id="autopilot")
    scheduler.add_job(llm_signal_pass, CronTrigger(minute="7"), id="llm_pass")
    scheduler.add_job(ml_learning_pass, CronTrigger(minute="12", hour="*/6"), id="ml_learning")
    scheduler.add_job(equity_snapshot, CronTrigger(minute="55"), id="equity_curve")
    scheduler.add_job(reconcile_portfolio, CronTrigger(minute="*/5"), id="portfolio_reconcile")
    return scheduler
