"""FastAPI entrypoint. Wires routes + APScheduler lifespan."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI

from app.api import router
from app.auth import auth_guard, auth_router
from app.config import get_settings
from app.exchange.filters import filters
from app.exchange.ws_stream import live_prices
from app.llm import LLMReasoner
from app.logging_setup import configure_logging, get_logger
from app.scheduler import build_scheduler
from app.storage import storage
from app.trading.paper import paper_exchange
from app.trading.health import startup_report
from app.trading import risk_loop
from app.trading.watchdog import start_health_monitor, stop_health_monitor
from app.trading.startup import verify_before_trading

log = get_logger(__name__)


def _log_trading_mode(settings) -> None:
    """Make the live-trading flags and the BUY kill switch visible at boot."""
    log.critical(
        "TRADING MODE: live_mode=%s dry_run=%s paper_trading=%s live_buys_enabled=%s",
        settings.live_mode, settings.dry_run, settings.paper_trading,
        settings.live_buys_enabled,
    )
    if settings.live_mode and not settings.live_buys_enabled:
        log.critical(
            "LIVE_MODE is true but LIVE_BUYS_ENABLED is false — all new BUY orders "
            "will be rejected by the kill switch"
        )
    elif settings.live_mode and settings.live_buys_enabled:
        log.critical("LIVE TRADING ARMED — new BUY orders are allowed")


async def _log_price_diagnostic(settings) -> None:
    """Log endpoints and a live BTCUSDT price so the operator can see data is real."""
    log.info(
        "Binance endpoints: binance_base_url=%s binance_ws_url=%s",
        settings.binance_base_url, settings.binance_ws_url,
    )
    try:
        from app.exchange.client import BinanceUSClient

        price = await BinanceUSClient().ticker_price("BTCUSDT")
        log.info("Live price check: BTCUSDT=%s (source=%s)",
                 price, settings.binance_base_url)
    except Exception as exc:  # noqa: BLE001
        log.warning("BTCUSDT price diagnostic failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
    storage.kv_set("process_started_at", datetime.now(timezone.utc).isoformat())
    # Fail loud if the LLM is wired into the trading loop but disabled (e.g. the
    # configured provider has no API key/token on this host). Without this guard
    # a missing GITHUB_TOKEN/DEEPSEEK_API_KEY silently turns every LLM vote into
    # HOLD/0.0 while the operator believes LLM reasoning is active.
    if settings.llm_in_trading_loop:
        reasoner = LLMReasoner()
        if not reasoner.enabled:
            log.warning(
                "LLM_IN_TRADING_LOOP is true but the LLM reasoner is DISABLED "
                "(provider=%s has no API key/token). LLM votes will be HOLD/0.0. "
                "Set a key for this provider or unset LLM_IN_TRADING_LOOP.",
                reasoner.provider,
            )
        else:
            log.info("LLM reasoner active in trading loop; provider=%s", reasoner.provider)
    _log_trading_mode(settings)
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

    try:
        await verify_before_trading()
    except Exception as exc:  # noqa: BLE001
        log.critical("startup safety verification failed: %s", exc)
    # Seed paper account on first run.
    try:
        if settings.paper_trading:
            paper_exchange.ensure_seeded()
    except Exception as exc:  # noqa: BLE001
        log.exception("paper account seed failed: %s", exc)

    # Start the live price websocket cache (best-effort; falls back to REST).
    try:
        live_prices.start()
    except Exception as exc:  # noqa: BLE001
        log.warning("live price stream start failed: %s", exc)

    # Independent risk-management loop (stop-loss/take-profit/trailing), fully
    # decoupled from the scheduler/strategy tick — see app/trading/risk_loop.py
    # for why. Must start before/independent of the scheduler so a stuck
    # scheduler job can never block stop-losses from firing.
    try:
        risk_loop.start()
    except Exception as exc:  # noqa: BLE001
        log.exception("independent risk loop start failed: %s", exc)

    scheduler = None
    try:
        scheduler = build_scheduler()
        scheduler.start()
        log.info("scheduler started; jobs=%s", [j.id for j in scheduler.get_jobs()])
        start_health_monitor(scheduler)
    except Exception as exc:  # noqa: BLE001
        log.exception("scheduler start failed: %s", exc)

    try:
        await startup_report()
    except Exception as exc:  # noqa: BLE001
        log.exception("startup report failed: %s", exc)

    await _log_price_diagnostic(settings)

    try:
        yield
    finally:
        try:
            await stop_health_monitor()
        except Exception as exc:  # noqa: BLE001
            log.warning("health monitor stop failed: %s", exc)
        try:
            await risk_loop.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("independent risk loop stop failed: %s", exc)
        try:
            await live_prices.stop()
        except Exception as exc:  # noqa: BLE001
            log.warning("live price stream stop failed: %s", exc)
        if scheduler is not None and scheduler.running:
            try:
                scheduler.shutdown(wait=False)
                log.info("scheduler stopped")
            except Exception as exc:  # noqa: BLE001
                log.warning("scheduler shutdown failed: %s", exc)


app = FastAPI(title="AI Crypto Trading Machine", lifespan=lifespan)

# Session-based auth guard. Replaces the previous HTTP Basic middleware.
app.middleware("http")(auth_guard)


@app.get("/healthz", include_in_schema=False)
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


app.include_router(auth_router)
app.include_router(router)
