"""Auto-pilot controller: single source of truth for is-trading-on/off.

Holds runtime state. The scheduler calls `autopilot.tick()` every cycle;
`tick()` does nothing unless the user has explicitly hit Start.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from app.agents import run_all_agents
from app.config import get_settings
from app.exchange import BinanceUSClient, OrderSide, OrderType
from app.logging_setup import get_logger
from app.signals import SignalAction
from app.trading.portfolio import portfolio_snapshot

log = get_logger(__name__)


@dataclass
class AutopilotState:
    running: bool = False
    started_at: Optional[datetime] = None
    last_tick_at: Optional[datetime] = None
    last_action: str = ""
    last_error: str = ""
    trades_executed: int = 0
    starting_balance_usdt: Optional[Decimal] = None


class Autopilot:
    """Singleton trading controller."""

    def __init__(self) -> None:
        self.state = AutopilotState()
        self._lock = asyncio.Lock()

    # ── lifecycle ────────────────────────────────────────────────────
    async def start(self) -> AutopilotState:
        s = get_settings()
        if not (s.binance_api_key.get_secret_value()
                and s.binance_api_secret.get_secret_value()):
            raise RuntimeError(
                "Binance.US API credentials are not set. "
                "Save them on the Settings page first."
            )
        # Flip safety toggles OFF so place_order goes live.
        s.dry_run = False
        s.paper_trading = False

        # Capture baseline portfolio value for P&L tracking.
        try:
            snap = await portfolio_snapshot()
            self.state.starting_balance_usdt = snap["total_usdt"]
        except Exception as exc:  # noqa: BLE001
            self.state.last_error = f"baseline fetch failed: {exc}"
            log.warning("baseline portfolio fetch failed: %s", exc)

        self.state.running = True
        self.state.started_at = datetime.now(timezone.utc)
        self.state.trades_executed = 0
        self.state.last_action = "started"
        log.warning("AUTOPILOT STARTED — live trading enabled")
        return self.state

    async def stop_and_liquidate(self) -> AutopilotState:
        """Stop the loop AND market-sell every non-USDT balance."""
        self.state.running = False
        self.state.last_action = "stopping"
        client = BinanceUSClient()
        try:
            await client.liquidate_all()
            self.state.last_action = "stopped & liquidated"
            log.warning("AUTOPILOT STOPPED — portfolio liquidated to USDT")
        except Exception as exc:  # noqa: BLE001
            self.state.last_error = str(exc)
            self.state.last_action = "stopped (liquidate FAILED)"
            log.exception("Liquidation failed: %s", exc)
        finally:
            # Re-engage safety toggles after we're done.
            s = get_settings()
            s.dry_run = True
            s.paper_trading = True
        return self.state

    # ── scheduled tick ───────────────────────────────────────────────
    async def tick(self) -> None:
        """Called by the scheduler. No-op when stopped."""
        if not self.state.running:
            return
        if self._lock.locked():
            log.info("autopilot tick skipped — previous tick still running")
            return
        async with self._lock:
            self.state.last_tick_at = datetime.now(timezone.utc)
            try:
                signals = await run_all_agents(use_llm=False)
            except Exception as exc:  # noqa: BLE001
                self.state.last_error = f"agent run failed: {exc}"
                log.exception("autopilot agent run failed")
                return
            await self._execute(signals)

    async def _execute(self, signals) -> None:
        """Naive executor: market BUY on high-confidence BUYs, market SELL on SELLs.

        Position sizing is intentionally minimal here — replace with the
        Kelly / risk-cap pipeline in `app/sizing/` when that lands.
        """
        s = get_settings()
        if not signals:
            return
        client = BinanceUSClient()
        try:
            account = await client.account()
        except Exception as exc:  # noqa: BLE001
            self.state.last_error = f"account fetch failed: {exc}"
            log.exception("autopilot account fetch failed")
            return

        balances = {b["asset"]: Decimal(b["free"]) for b in account.get("balances", [])}
        usdt_free = balances.get("USDT", Decimal("0"))

        # Cash budget per trade = max_position_pct of USDT
        per_trade_usdt = usdt_free * Decimal(str(s.max_position_pct))

        for symbol, sig in signals.items():
            if sig.confidence < 0.6:
                continue
            try:
                if sig.action == SignalAction.BUY and per_trade_usdt > 1:
                    price = await client.ticker_price(symbol)
                    qty = (per_trade_usdt / price).quantize(Decimal("0.0001"))
                    if qty > 0:
                        await client.place_order(
                            symbol=symbol,
                            side=OrderSide.BUY,
                            type=OrderType.MARKET,
                            quantity=qty,
                        )
                        self.state.trades_executed += 1
                elif sig.action == SignalAction.SELL:
                    base = symbol.replace("USDT", "")
                    free = balances.get(base, Decimal("0"))
                    if free > 0:
                        await client.place_order(
                            symbol=symbol,
                            side=OrderSide.SELL,
                            type=OrderType.MARKET,
                            quantity=free,
                        )
                        self.state.trades_executed += 1
            except Exception as exc:  # noqa: BLE001
                self.state.last_error = f"{symbol}: {exc}"
                log.warning("execute failed %s: %s", symbol, exc)


autopilot = Autopilot()
