"""Scheduler wiring. Single AsyncIOScheduler shared by the FastAPI app."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.agents import run_all_agents
from app.config import SYMBOLS, TIMEFRAMES, get_settings
from app.data import OHLCVRepository
from app.llm import LLMReasoner
from app.logging_setup import get_logger
from app.regime import run_learning_cycle
from app.storage import storage
from app.trading import autopilot
from app.trading.portfolio import portfolio_snapshot

log = get_logger(__name__)

_LLM_KV_KEY = "llm_signals"
_LLM_META_KEY = "llm_meta"


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


async def equity_snapshot() -> None:
    """Record current portfolio total. Runs whether autopilot is running or not."""
    mode = autopilot.state.mode
    try:
        snap = await portfolio_snapshot(mode=mode)
    except Exception as exc:  # noqa: BLE001
        log.warning("equity snapshot failed: %s", exc)
        return
    total = float(snap["total_usdt"])
    cash = float(snap["usdt_cash"])
    invested = total - cash
    storage.record_equity_snapshot(
        mode=mode, total_usdt=total, cash_usdt=cash, invested_usdt=invested,
    )


async def llm_signal_pass() -> None:
    """Hourly LLM-only pass. Off the order-placement hot path.

    Runs `run_all_agents(use_llm=True)` and stashes the aggregated signals in
    the KV store so the dashboard can show what the LLM is currently saying.
    The autopilot tick continues to run on the rule-based agents only — by
    design — but operators can read this card to sanity-check.
    """
    s = get_settings()
    if (s.llm_provider or "none").lower() == "none":
        return
    reasoner = LLMReasoner()
    if not reasoner.enabled:
        log.info("LLM disabled (no key for provider=%s); skipping LLM pass", reasoner.provider)
        return
    try:
        signals = await run_all_agents(use_llm=True)
    except Exception as exc:  # noqa: BLE001
        log.exception("llm pass failed: %s", exc)
        storage.kv_set(_LLM_META_KEY, {
            "provider": reasoner.provider,
            "last_run": datetime.now(timezone.utc).isoformat(),
            "last_error": str(exc),
            "count": 0,
        })
        return
    payload = {
        sym: {
            "action": sig.action.value,
            "confidence": sig.confidence,
            "rationale": sig.rationale,
            "agents": list(sig.contributing_agents),
        }
        for sym, sig in signals.items()
    }
    storage.kv_set(_LLM_KV_KEY, payload)
    storage.kv_set(_LLM_META_KEY, {
        "provider": reasoner.provider,
        "last_run": datetime.now(timezone.utc).isoformat(),
        "last_error": "",
        "count": len(payload),
    })
    log.info("llm pass complete: provider=%s symbols=%d", reasoner.provider, len(payload))


async def ml_learning_pass() -> None:
    """Label matured signals and periodically retrain the quality model."""
    s = get_settings()
    if not s.ml_learning_enabled:
        return
    try:
        result = await run_learning_cycle()
    except Exception as exc:  # noqa: BLE001
        log.exception("ml learning pass failed: %s", exc)
        return
    log.info("ml learning pass: %s", result)


def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(refresh_market_data, CronTrigger(minute="*/15"), id="market_data")
    scheduler.add_job(autopilot_tick, CronTrigger(minute="2,17,32,47"), id="autopilot")
    scheduler.add_job(llm_signal_pass, CronTrigger(minute="7"), id="llm_pass")
    scheduler.add_job(ml_learning_pass, CronTrigger(minute="12", hour="*/6"), id="ml_learning")
    scheduler.add_job(equity_snapshot, CronTrigger(minute="55"), id="equity_curve")
    return scheduler

