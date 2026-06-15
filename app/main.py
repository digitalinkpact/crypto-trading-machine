"""FastAPI entrypoint. Wires routes + APScheduler lifespan."""
from __future__ import annotations

from contextlib import asynccontextmanager

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

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    settings = get_settings()
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

    # Start the live price websocket cache (best-effort; falls back to REST).
    try:
        live_prices.start()
    except Exception as exc:  # noqa: BLE001
        log.warning("live price stream start failed: %s", exc)

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
