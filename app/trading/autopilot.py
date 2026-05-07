"""Auto-pilot controller.

Single source of truth for "are we trading?". Runs in either PAPER or LIVE mode
based on `settings.paper_trading`. Both modes share the SQLite store, so the
agent-attribution stats earned during paper trading carry straight into live.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from app.agents import run_all_agents
from app.config import get_settings
from app.exchange import BinanceUSClient, Order, OrderSide, OrderType
from app.exchange.filters import filters
from app.logging_setup import get_logger
from app.signals import SignalAction
from app.storage import storage
from app.trading.paper import paper_exchange
from app.trading.portfolio import portfolio_snapshot

log = get_logger(__name__)

_STATE_KEY = "autopilot_state"
_COOLDOWN_MIN = 60  # minutes between BUYs on the same symbol
_MIN_CONFIDENCE = 0.6


@dataclass
class AutopilotState:
    running: bool = False
    mode: str = "paper"  # "paper" | "live"
    started_at: Optional[datetime] = None
    last_tick_at: Optional[datetime] = None
    last_action: str = ""
    last_error: str = ""
    trades_executed: int = 0
    starting_balance_usdt: Optional[Decimal] = None
    cooldowns: dict[str, str] = field(default_factory=dict)  # symbol -> iso ts

    def to_dict(self) -> dict:
        return {
            "running": self.running,
            "mode": self.mode,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "last_tick_at": self.last_tick_at.isoformat() if self.last_tick_at else None,
            "last_action": self.last_action,
            "last_error": self.last_error,
            "trades_executed": self.trades_executed,
            "starting_balance_usdt": (
                str(self.starting_balance_usdt) if self.starting_balance_usdt else None
            ),
            "cooldowns": self.cooldowns,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AutopilotState":
        s = cls()
        s.running = bool(d.get("running"))
        s.mode = d.get("mode") or "paper"
        s.started_at = _parse_dt(d.get("started_at"))
        s.last_tick_at = _parse_dt(d.get("last_tick_at"))
        s.last_action = d.get("last_action") or ""
        s.last_error = d.get("last_error") or ""
        s.trades_executed = int(d.get("trades_executed") or 0)
        sb = d.get("starting_balance_usdt")
        s.starting_balance_usdt = Decimal(sb) if sb else None
        s.cooldowns = dict(d.get("cooldowns") or {})
        return s


def _parse_dt(v: Optional[str]) -> Optional[datetime]:
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


class Autopilot:
    """Singleton trading controller."""

    def __init__(self) -> None:
        # Restore persisted state across restarts.
        saved = storage.kv_get(_STATE_KEY) or {}
        self.state = AutopilotState.from_dict(saved) if saved else AutopilotState()
        # Always sync mode with current settings on boot.
        self.state.mode = "paper" if get_settings().paper_trading else "live"
        self._lock = asyncio.Lock()

    # ── persistence ────────────────────────────────────────────────────
    def _save(self) -> None:
        storage.kv_set(_STATE_KEY, self.state.to_dict())

    # ── lifecycle ──────────────────────────────────────────────────────
    async def start(self) -> AutopilotState:
        s = get_settings()
        self.state.mode = "paper" if s.paper_trading else "live"

        if self.state.mode == "live":
            if not (s.binance_api_key.get_secret_value()
                    and s.binance_api_secret.get_secret_value()):
                raise RuntimeError(
                    "Live trading requires Binance.US API credentials. "
                    "Save them on Settings, or switch to Paper mode."
                )
        else:
            paper_exchange.ensure_seeded()

        # Capture baseline portfolio value.
        try:
            snap = await portfolio_snapshot(mode=self.state.mode)
            self.state.starting_balance_usdt = snap["total_usdt"]
        except Exception as exc:  # noqa: BLE001
            self.state.last_error = f"baseline fetch failed: {exc}"
            log.warning("baseline portfolio fetch failed: %s", exc)

        self.state.running = True
        self.state.started_at = datetime.now(timezone.utc)
        self.state.trades_executed = 0
        self.state.last_error = ""
        self.state.last_action = f"started ({self.state.mode})"
        log.warning("AUTOPILOT STARTED — mode=%s", self.state.mode)
        self._save()
        return self.state

    async def stop_and_liquidate(self) -> AutopilotState:
        """Stop the loop AND market-sell every non-USDT balance back to USDT."""
        self.state.running = False
        self.state.last_action = "stopping"
        self._save()
        try:
            if self.state.mode == "paper":
                await paper_exchange.liquidate_all()
            else:
                client = BinanceUSClient()
                await client.liquidate_all()
            self.state.last_action = f"stopped & liquidated ({self.state.mode})"
            log.warning("AUTOPILOT STOPPED — liquidated to USDT (%s)", self.state.mode)
        except Exception as exc:  # noqa: BLE001
            self.state.last_error = str(exc)
            self.state.last_action = "stopped (liquidate FAILED)"
            log.exception("Liquidation failed: %s", exc)
        self._save()
        return self.state

    # ── scheduled tick ─────────────────────────────────────────────────
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
                self._save()
                return
            try:
                await self._execute(signals)
            finally:
                self._save()

    # ── execution ──────────────────────────────────────────────────────
    async def _execute(self, signals) -> None:
        if not signals:
            return
        s = get_settings()
        try:
            snap = await portfolio_snapshot(mode=self.state.mode)
        except Exception as exc:  # noqa: BLE001
            self.state.last_error = f"portfolio fetch failed: {exc}"
            log.exception("autopilot portfolio fetch failed")
            return

        usdt_free = Decimal(str(snap["usdt_cash"]))
        balances: dict[str, Decimal] = {
            asset: Decimal(str(qty)) for asset, qty in snap["all_balances"].items()
        }
        per_trade_usdt = usdt_free * Decimal(str(s.max_position_pct))
        now = datetime.now(timezone.utc)

        for symbol, sig in signals.items():
            if sig.confidence < _MIN_CONFIDENCE:
                continue
            if not filters.is_listed(symbol):
                continue
            try:
                if sig.action == SignalAction.BUY:
                    if self._on_cooldown(symbol, now):
                        continue
                    if per_trade_usdt <= 1:
                        continue
                    await self._place_buy(symbol, sig, per_trade_usdt)
                    self.state.cooldowns[symbol] = now.isoformat()
                elif sig.action == SignalAction.SELL:
                    base = symbol.replace("USDT", "")
                    free = balances.get(base, Decimal("0"))
                    if free > 0:
                        await self._place_sell(symbol, sig, free)
            except Exception as exc:  # noqa: BLE001
                self.state.last_error = f"{symbol}: {exc}"
                log.warning("execute failed %s: %s", symbol, exc)

    def _on_cooldown(self, symbol: str, now: datetime) -> bool:
        ts = self.state.cooldowns.get(symbol)
        if not ts:
            return False
        last = _parse_dt(ts)
        if not last:
            return False
        return (now - last) < timedelta(minutes=_COOLDOWN_MIN)

    async def _price(self, symbol: str) -> Decimal:
        if self.state.mode == "paper":
            return await paper_exchange.ticker_price(symbol)
        return await BinanceUSClient().ticker_price(symbol)

    async def _place_buy(self, symbol: str, sig, per_trade_usdt: Decimal) -> None:
        price = await self._price(symbol)
        raw_qty = per_trade_usdt / price
        qty = filters.round_qty(symbol, raw_qty)
        if qty <= 0 or not filters.meets_min(symbol, qty, price):
            log.info("skip %s BUY: filters reject qty=%s price=%s", symbol, qty, price)
            return
        agents = list(getattr(sig, "contributing_agents", []) or [])
        await self._submit(symbol, OrderSide.BUY, qty, agents)

    async def _place_sell(self, symbol: str, sig, free: Decimal) -> None:
        price = await self._price(symbol)
        qty = filters.round_qty(symbol, free)
        if qty <= 0 or not filters.meets_min(symbol, qty, price):
            log.info("skip %s SELL: filters reject qty=%s", symbol, qty)
            return
        agents = list(getattr(sig, "contributing_agents", []) or [])
        await self._submit(symbol, OrderSide.SELL, qty, agents)

    async def _submit(
        self, symbol: str, side: OrderSide, qty: Decimal, agents: list[str]
    ) -> Optional[Order]:
        if self.state.mode == "paper":
            order = await paper_exchange.place_order(
                symbol=symbol, side=side, quantity=qty, agents=agents,
            )
        else:
            client = BinanceUSClient()
            order = await client.place_order(
                symbol=symbol, side=side, type=OrderType.MARKET, quantity=qty,
            )
            try:
                price = order.avg_fill_price or order.price or await self._price(symbol)
                storage.record_order(
                    mode="live", symbol=symbol, side=side.value,
                    qty=qty, price=price,
                    client_order_id=order.client_order_id, agents=agents,
                )
                if side is OrderSide.BUY:
                    storage.open_position(
                        symbol=symbol, mode="live", qty=qty,
                        entry_price=price, agents=agents,
                    )
                else:
                    storage.close_position(symbol=symbol, exit_price=price)
            except Exception as exc:  # noqa: BLE001
                log.warning("storage write failed for live order %s: %s", symbol, exc)
        self.state.trades_executed += 1
        return order


autopilot = Autopilot()
