"""Live price cache backed by the Binance.US websocket stream.

Binance.US REST `klines` give us the 500-bar history the indicators need, but
that history is only refreshed every ~15 minutes. This module subscribes to the
combined `!miniTicker@arr` stream (one connection, every symbol, ~1 update/sec)
and keeps an in-memory `{symbol: (price, ts)}` map so execution can price fills
against a near-real-time last trade instead of a 15-minute-old candle close.

Design notes:
  * One connection for the whole universe — `!miniTicker@arr` pushes an array of
    every actively-traded symbol, so we never have to manage per-symbol subs.
  * Best-effort and self-healing: reconnects with capped backoff. If the socket
    is down, `get_fresh()` returns None and callers fall back to REST.
  * Lives in `app/exchange/` because it is the ONLY layer allowed to talk to the
    exchange directly (websocket included).
"""
from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal, InvalidOperation
from typing import Optional

import websockets

from app.config import Settings, get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)

# All-market mini-ticker array stream. One frame carries every symbol's 24h
# rolling stats; field `c` is the last (close) price, `s` is the symbol.
_STREAM_PATH = "/ws/!miniTicker@arr"


class LivePriceCache:
    """In-memory last-price cache fed by a single combined websocket stream."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._prices: dict[str, tuple[Decimal, float]] = {}
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._connected = False
        self._last_msg_at: float = 0.0

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        """Launch the background stream task (idempotent)."""
        if not self._settings.live_price_enabled:
            log.info("live price stream disabled (live_price_enabled=false)")
            return
        if self._task is not None and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="live-price-stream")
        log.info("live price stream starting: %s%s",
                 self._settings.binance_ws_url, _STREAM_PATH)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        self._connected = False

    # ── reads ────────────────────────────────────────────────────────────
    def get_fresh(self, symbol: str, max_age: Optional[float] = None) -> Optional[Decimal]:
        """Return the cached last price if newer than `max_age` seconds, else None."""
        entry = self._prices.get(symbol.upper())
        if not entry:
            return None
        price, ts = entry
        age_limit = max_age if max_age is not None else self._settings.live_price_max_age_seconds
        if (time.time() - ts) > age_limit:
            return None
        return price

    @property
    def connected(self) -> bool:
        return self._connected

    def status(self) -> dict:
        return {
            "enabled": self._settings.live_price_enabled,
            "running": self._running,
            "connected": self._connected,
            "symbols_cached": len(self._prices),
            "last_msg_age_s": (round(time.time() - self._last_msg_at, 1)
                               if self._last_msg_at else None),
        }

    # ── internals ────────────────────────────────────────────────────────
    async def _run(self) -> None:
        url = f"{self._settings.binance_ws_url}{_STREAM_PATH}"
        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                    self._connected = True
                    backoff = 1.0
                    log.info("live price stream connected")
                    async for raw in ws:
                        if not self._running:
                            break
                        self._ingest(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._connected = False
                log.warning("live price stream dropped (%s); reconnecting in %.0fs",
                            exc, backoff)
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    raise
                backoff = min(backoff * 2, 60.0)
        self._connected = False

    def _ingest(self, raw: str | bytes) -> None:
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        # `!miniTicker@arr` delivers a list; guard against single-object frames.
        items = payload if isinstance(payload, list) else [payload]
        now = time.time()
        for item in items:
            if not isinstance(item, dict):
                continue
            sym = item.get("s")
            last = item.get("c")
            if not sym or last is None:
                continue
            try:
                price = Decimal(str(last))
            except (InvalidOperation, ValueError):
                continue
            if price > 0:
                self._prices[str(sym).upper()] = (price, now)
        self._last_msg_at = now


# Process-wide singleton used by the autopilot and dashboards.
live_prices = LivePriceCache()
