"""Auto-pilot controller.

Single source of truth for "are we trading?". Runs in either PAPER or LIVE mode
based on `settings.paper_trading`. Both modes share the SQLite store, so the
agent-attribution stats earned during paper trading carry straight into live.

Each tick:
  1. Run risk gates over all open positions (stop-loss / take-profit / trailing /
     max-hold). Force-exit anything that hit a rule.
  2. Check drawdown circuit breaker. If tripped, skip new BUYs.
  3. Run agents → aggregator → execute high-confidence signals subject to
     cooldown, max_open_positions, max_long_exposure, volatility-scaled sizing.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from app.agents import run_all_agents
from app.config import Timeframe, get_settings
from app.exchange import BinanceUSClient, Order, OrderSide, OrderType
from app.exchange.filters import filters
from app.logging_setup import get_logger
from app.signals import SignalAction
from app.storage import storage
from app.trading import risk
from app.trading.paper import paper_exchange
from app.trading.portfolio import portfolio_snapshot

log = get_logger(__name__)

_STATE_KEY = "autopilot_state"
_SKIP_STATS_KEY = "autopilot_skip_stats"
_LAST_TICK_DEBUG_KEY = "autopilot_last_tick_debug"


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
        # Kick an immediate tick in the background so the user doesn't wait up
        # to 15 min for the next cron slot before any trade can fire.
        try:
            asyncio.create_task(self.tick())
            log.info("autopilot first tick scheduled immediately after Start")
        except RuntimeError as exc:  # no running loop — extremely unlikely here
            log.warning("could not schedule immediate tick: %s", exc)
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

            # 1. Risk gates — stop-loss / take-profit / trailing / max-hold.
            #    Run BEFORE agents so we exit losers regardless of new signals.
            try:
                await self._run_risk_gates()
            except Exception as exc:  # noqa: BLE001
                log.exception("risk gate run failed: %s", exc)

            # 2. Drawdown circuit breaker.
            try:
                breaker_tripped = await self._check_circuit_breaker()
            except Exception as exc:  # noqa: BLE001
                log.warning("circuit breaker check failed: %s", exc)
                breaker_tripped = False

            # 3. Agent signals → execute (skip BUYs if breaker tripped).
            try:
                signals = await run_all_agents(use_llm=get_settings().llm_in_trading_loop)
            except Exception as exc:  # noqa: BLE001
                self.state.last_error = f"agent run failed: {exc}"
                log.exception("autopilot agent run failed")
                self._save()
                return
            try:
                await self._execute(signals, allow_buys=not breaker_tripped)
            finally:
                self._save()

    # ── risk gates ─────────────────────────────────────────────────────
    async def _run_risk_gates(self) -> None:
        positions = [p for p in storage.all_positions() if p["mode"] == self.state.mode]
        if not positions:
            return
        prices: dict[str, Decimal] = {}
        for pos in positions:
            try:
                prices[pos["symbol"]] = await self._price(pos["symbol"])
            except Exception as exc:  # noqa: BLE001
                log.warning("price fetch failed for %s: %s", pos["symbol"], exc)
        exits = risk.evaluate_exits(positions=positions, prices=prices)
        for ex in exits:
            try:
                price = prices.get(ex.symbol) or await self._price(ex.symbol)
                qty = filters.round_qty(ex.symbol, ex.qty)
                if qty <= 0 or not filters.meets_min(ex.symbol, qty, price):
                    log.info("risk-exit %s skipped: filters reject qty=%s", ex.symbol, qty)
                    continue
                log.warning("RISK EXIT %s reason=%s qty=%s price=%s",
                            ex.symbol, ex.reason, qty, price)
                await self._submit(ex.symbol, OrderSide.SELL, qty, [f"risk:{ex.reason}"])
                risk.clear_hwm(ex.symbol)
            except Exception as exc:  # noqa: BLE001
                log.exception("risk-exit failed for %s: %s", ex.symbol, exc)

    async def _check_circuit_breaker(self) -> bool:
        try:
            snap = await portfolio_snapshot(mode=self.state.mode)
        except Exception as exc:  # noqa: BLE001
            log.warning("breaker portfolio fetch failed: %s", exc)
            return False
        tripped, dd = risk.is_circuit_breaker_tripped(
            starting_balance=self.state.starting_balance_usdt,
            current_balance=Decimal(str(snap["total_usdt"])),
        )
        if tripped:
            self.state.last_error = (
                f"DRAWDOWN BREAKER TRIPPED at {dd:.1%} — new BUYs halted"
            )
            log.warning(self.state.last_error)
        return tripped

    # ── execution ──────────────────────────────────────────────────────
    async def _execute(self, signals, *, allow_buys: bool = True) -> None:
        skip_counter: Counter[str] = Counter()
        tick_debug: dict[str, dict] = {}

        def _bump(reason: str, sym: str = "", detail: str = "") -> None:
            skip_counter[reason] += 1
            if sym:
                tick_debug[sym] = {"reason": reason, "detail": detail}

        if not signals:
            _bump("no_signals")
            self._persist_skip_stats(skip_counter, tick_debug, total=0)
            log.info("autopilot tick: no aggregated signals produced")
            return
        s = get_settings()
        try:
            snap = await portfolio_snapshot(mode=self.state.mode)
        except Exception as exc:  # noqa: BLE001
            self.state.last_error = f"portfolio fetch failed: {exc}"
            log.exception("autopilot portfolio fetch failed")
            return

        usdt_free = Decimal(str(snap["usdt_cash"]))
        total_eq = Decimal(str(snap["total_usdt"]))
        long_exposure_pct = float(
            (total_eq - usdt_free) / total_eq if total_eq > 0 else Decimal("0")
        )
        balances: dict[str, Decimal] = {
            asset: Decimal(str(qty)) for asset, qty in snap["all_balances"].items()
        }
        open_positions = [
            p for p in storage.all_positions() if p["mode"] == self.state.mode
        ]
        open_count = len(open_positions)
        held_symbols = {p["symbol"] for p in open_positions}
        now = datetime.now(timezone.utc)
        cooldown = timedelta(minutes=s.buy_cooldown_minutes)

        # ML quality gate — load the learned model once per tick. Trades whose
        # predicted win-probability is below the threshold are skipped. Loaded
        # lazily so a freshly retrained model is picked up next tick.
        ml_model = None
        if s.ml_gate_enabled:
            try:
                artifact = storage.load_model_artifact("signal_quality_v1")
                ml_model = artifact["model"] if artifact else None
            except Exception as exc:  # noqa: BLE001
                log.debug("ml gate model load failed: %s", exc)
                ml_model = None

        for symbol, sig in signals.items():
            if sig.action == SignalAction.HOLD:
                _bump("action_hold", symbol, f"conf={sig.confidence:.2f}")
                continue
            if sig.confidence < s.min_signal_confidence:
                _bump("low_confidence", symbol,
                      f"{sig.action.value} conf={sig.confidence:.2f} < {s.min_signal_confidence}")
                continue
            if ml_model is not None and sig.action in (SignalAction.BUY, SignalAction.SELL):
                proba = await self._ml_win_proba(ml_model, symbol, sig)
                if proba is not None and proba < s.ml_gate_threshold:
                    _bump("ml_gate", symbol,
                          f"{sig.action.value} proba={proba:.2f} < {s.ml_gate_threshold}")
                    continue
            if not filters.is_listed(symbol):
                _bump("not_listed", symbol)
                continue
            try:
                if sig.action in (SignalAction.BUY, SignalAction.SELL):
                    await self._record_signal_event(symbol, sig)

                if sig.action == SignalAction.BUY:
                    if not allow_buys:
                        _bump("breaker_tripped", symbol)
                        continue
                    if symbol in held_symbols:
                        _bump("already_held", symbol)
                        continue  # don't pyramid into existing position
                    if self._on_cooldown(symbol, now, cooldown):
                        _bump("cooldown", symbol)
                        continue
                    ok, why = risk.can_open_new_position(
                        open_positions=open_count,
                        long_exposure_pct=long_exposure_pct,
                    )
                    if not ok:
                        _bump("risk_cap", symbol, why)
                        log.info("skip %s BUY: %s", symbol, why)
                        continue

                    # Volatility-scaled sizing.
                    atr_pct = await self._atr_pct(symbol)
                    eff_pct = risk.volatility_scaled_pct(s.max_position_pct, atr_pct)
                    per_trade_usdt = usdt_free * Decimal(str(eff_pct))
                    # Enforce $10 minimum per trade
                    if per_trade_usdt < 10:
                        if usdt_free >= 10:
                            per_trade_usdt = Decimal("10")
                        else:
                            _bump("insufficient_usdt", symbol,
                                  f"per_trade={per_trade_usdt:.4f} cash={usdt_free:.2f} eff={eff_pct:.4f}")
                            continue

                    placed = await self._place_buy(symbol, sig, per_trade_usdt)
                    if not placed:
                        _bump("filter_reject_buy", symbol)
                        continue
                    self.state.cooldowns[symbol] = now.isoformat()
                    open_count += 1
                    held_symbols.add(symbol)
                    skip_counter["executed_buy"] += 1
                    # Approximate exposure update so subsequent BUYs see the new total.
                    long_exposure_pct = min(
                        1.0,
                        long_exposure_pct + float(per_trade_usdt / total_eq) if total_eq > 0 else 0,
                    )
                elif sig.action == SignalAction.SELL:
                    base = symbol.removesuffix("USDT")
                    free = balances.get(base, Decimal("0"))
                    if free > 0:
                        placed = await self._place_sell(symbol, sig, free)
                        if placed:
                            skip_counter["executed_sell"] += 1
                            risk.clear_hwm(symbol)
                        else:
                            _bump("filter_reject_sell", symbol)
                    else:
                        _bump("sell_no_balance", symbol)
            except Exception as exc:  # noqa: BLE001
                self.state.last_error = f"{symbol}: {exc}"
                log.warning("execute failed %s: %s", symbol, exc)
                _bump("exception", symbol, str(exc))
        self._persist_skip_stats(skip_counter, tick_debug, total=len(signals))

    async def _ml_win_proba(self, model, symbol: str, sig) -> Optional[float]:
        """Predicted win-probability for a signal from the learned quality model.

        Feature order MUST match `_rows_to_xy` in app/regime/trainer.py:
        [confidence, atr_pct, rsi_14, ema_gap_pct, agent_count, tf_weight, action].
        Returns None if features can't be built (caller treats None as no opinion).
        """
        try:
            import numpy as np
            atr_pct, rsi_14, ema_gap_pct = await self._feature_snapshot(symbol)
            tf = getattr(sig.timeframe, "value", Timeframe.D1.value)
            tf_weight = {"1h": 1.0, "4h": 1.5, "1d": 2.5, "1w": 4.0}.get(tf, 1.0)
            features = np.asarray([[
                float(sig.confidence),
                float(atr_pct if atr_pct is not None else 0.0),
                float(rsi_14 if rsi_14 is not None else 50.0),
                float(ema_gap_pct if ema_gap_pct is not None else 0.0),
                float(len(getattr(sig, "contributing_agents", ()) or ())),
                tf_weight,
                1.0 if sig.action == SignalAction.BUY else 0.0,
            ]], dtype=float)
            return float(model.predict_proba(features)[0, 1])
        except Exception as exc:  # noqa: BLE001
            log.debug("ml gate proba failed for %s: %s", symbol, exc)
            return None

    async def _record_signal_event(self, symbol: str, sig) -> None:
        """Best-effort feature snapshot used by the offline trainer."""
        s = get_settings()
        if not s.ml_learning_enabled:
            return
        try:
            price = await self._price(symbol)
            atr_pct, rsi_14, ema_gap_pct = await self._feature_snapshot(symbol)
            storage.record_signal_event(
                mode=self.state.mode,
                symbol=symbol,
                timeframe=getattr(sig.timeframe, "value", Timeframe.D1.value),
                action=sig.action.value,
                confidence=float(sig.confidence),
                entry_price=price,
                atr_pct=atr_pct,
                rsi_14=rsi_14,
                ema_gap_pct=ema_gap_pct,
                agent_count=len(getattr(sig, "contributing_agents", ()) or ()),
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("signal event capture failed %s: %s", symbol, exc)

    def _on_cooldown(self, symbol: str, now: datetime, cooldown: timedelta) -> bool:
        ts = self.state.cooldowns.get(symbol)
        if not ts:
            return False
        last = _parse_dt(ts)
        if not last:
            return False
        return (now - last) < cooldown

    def _persist_skip_stats(
        self,
        counter: Counter,
        tick_debug: dict,
        *,
        total: int,
    ) -> None:
        """Persist per-tick reason breakdown so /diagnose and operators can see why."""
        try:
            storage.kv_set(_SKIP_STATS_KEY, dict(counter))
            storage.kv_set(_LAST_TICK_DEBUG_KEY, {
                "ts": datetime.now(timezone.utc).isoformat(),
                "total_signals": total,
                "by_reason": dict(counter),
                "per_symbol": tick_debug,
            })
        except Exception as exc:  # noqa: BLE001
            log.debug("skip-stats persist failed: %s", exc)
        if counter:
            log.info(
                "autopilot tick result: signals=%d %s",
                total,
                ", ".join(f"{k}={v}" for k, v in counter.most_common()),
            )

    async def _atr_pct(self, symbol: str) -> Optional[float]:
        """Best-effort ATR% from cached daily OHLCV. None on any failure."""
        try:
            from app.config import Timeframe
            from app.data import OHLCVRepository
            from app.ta import add_indicators
            df = await OHLCVRepository().get(symbol, Timeframe.D1, refresh=False)
            df = add_indicators(df).dropna()
            if df.empty:
                return None
            last = df.iloc[-1]
            close = float(last["close"])
            atr = float(last["atr_14"])
            return (atr / close) if close > 0 else None
        except Exception as exc:  # noqa: BLE001
            log.debug("atr_pct fetch failed for %s: %s", symbol, exc)
            return None

    async def _feature_snapshot(self, symbol: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
        """Daily feature slice aligned with the signal timestamp."""
        try:
            from app.data import OHLCVRepository
            from app.ta import add_indicators

            df = await OHLCVRepository().get(symbol, Timeframe.D1, refresh=False)
            df = add_indicators(df).dropna()
            if df.empty:
                return None, None, None
            last = df.iloc[-1]
            close = float(last["close"])
            atr = float(last["atr_14"])
            ema20 = float(last["ema_20"])
            ema50 = float(last["ema_50"])
            atr_pct = (atr / close) if close > 0 else None
            ema_gap_pct = ((ema20 - ema50) / close) if close > 0 else None
            return atr_pct, float(last["rsi_14"]), ema_gap_pct
        except Exception as exc:  # noqa: BLE001
            log.debug("feature snapshot failed for %s: %s", symbol, exc)
            return None, None, None

    async def _price(self, symbol: str) -> Decimal:
        if self.state.mode == "paper":
            return await paper_exchange.ticker_price(symbol)
        return await BinanceUSClient().ticker_price(symbol)

    async def _place_buy(self, symbol: str, sig, per_trade_usdt: Decimal) -> bool:
        price = await self._price(symbol)
        raw_qty = per_trade_usdt / price
        qty = filters.round_qty(symbol, raw_qty)
        if qty <= 0 or not filters.meets_min(symbol, qty, price):
            log.info("skip %s BUY: filters reject qty=%s price=%s", symbol, qty, price)
            return False
        agents = list(getattr(sig, "contributing_agents", []) or [])
        await self._submit(symbol, OrderSide.BUY, qty, agents)
        return True

    async def _place_sell(self, symbol: str, sig, free: Decimal) -> bool:
        price = await self._price(symbol)
        qty = filters.round_qty(symbol, free)
        if qty <= 0 or not filters.meets_min(symbol, qty, price):
            log.info("skip %s SELL: filters reject qty=%s", symbol, qty)
            return False
        agents = list(getattr(sig, "contributing_agents", []) or [])
        await self._submit(symbol, OrderSide.SELL, qty, agents)
        return True

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
