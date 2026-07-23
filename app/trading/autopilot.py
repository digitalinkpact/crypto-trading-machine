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
import json
import os
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from app.agents import run_all_agents
from app.config import Timeframe, get_settings
from app.exchange import BinanceUSClient, Order, OrderSide, OrderStatus, OrderType
from app.exchange.derivatives import derivatives
from app.exchange.filters import filters
from app.exchange.orderbook import liquidity_gate
from app.exchange.ws_stream import live_prices
from app.logging_setup import get_logger
from app.regime import online_regime
from app.signals import SignalAction
from app.storage import storage
from app.trading.audit import trade_audit_logger
from app.trading import risk
from app.trading.paper import paper_exchange
from app.trading.portfolio import portfolio_snapshot
from app.trading.risk_manager import RiskManager

log = get_logger(__name__)

_STATE_KEY = "autopilot_state"
_SKIP_STATS_KEY = "autopilot_skip_stats"
_LAST_TICK_DEBUG_KEY = "autopilot_last_tick_debug"
_ML_GATE_STATS_KEY = "ml_gate_stats"


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


def _model_age_hours(trained_at: Optional[str]) -> Optional[float]:
    """Age in hours of a model artifact from its ISO ``trained_at`` stamp.

    Returns None if the timestamp is missing or unparseable (caller then treats
    the model as fresh — staleness can only relax, never tighten, the gate).
    """
    dt = _parse_dt(trained_at)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


def _jsonable(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


class Autopilot:
    """Singleton trading controller."""

    def __init__(self) -> None:
        # Restore persisted state across restarts.
        saved = storage.kv_get(_STATE_KEY) or {}
        self.state = AutopilotState.from_dict(saved) if saved else AutopilotState()
        # Always sync mode with current settings on boot.
        self.state.mode = "paper" if get_settings().paper_trading else "live"
        self._lock = asyncio.Lock()
        # Per-process identity for the cross-process tick mutex. Two app
        # instances sharing the SQLite DB get different owners, so only one can
        # hold the lock and execute a tick at a time.
        self._owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        # Last ML-gate model version we logged (avoids per-tick log spam).
        self._ml_logged_version: Optional[int] = None
        # Cached BTC market-regime verdict: (allowed, reason, monotonic_ts).
        # The regime is portfolio-wide, so it is computed once and reused for
        # every symbol within a tick instead of refetching BTC per candidate.
        self._market_regime_cache: Optional[tuple[bool, str, float]] = None
        self._orderbook_retry_tasks: dict[tuple[str, str], asyncio.Task] = {}

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
        # Cross-process guard: if another app instance (e.g. a stray dev server
        # sharing this DB) is mid-tick, skip. This prevents the duplicate-order
        # / negative-balance corruption that an in-process lock alone can't stop.
        # TTL is a crash safety net; we release explicitly in `finally`.
        if not storage.try_acquire_lock("autopilot_tick", ttl_seconds=300.0, owner=self._owner):
            log.info("autopilot tick skipped — another process holds the tick lock")
            return
        async with self._lock:
            self.state.last_tick_at = datetime.now(timezone.utc)
            try:
                # -1. Ensure paper mode has seeded balance (cold-start after restart).
                if self.state.mode == "paper":
                    try:
                        paper_exchange.ensure_seeded()
                    except Exception as exc:  # noqa: BLE001
                        log.warning("paper balance ensure-seeded failed: %s", exc)
                
                # 0. Ensure LOT_SIZE / MIN_NOTIONAL filters are loaded before any
                #    order is placed. `filters.meets_min` fails OPEN when the
                #    exchangeInfo cache is empty, so a tick that fires before the
                #    app-startup load would submit dust SELLs that Binance rejects
                #    with -1013 LOT_SIZE. The load is idempotent (no-op once
                #    loaded), so this just closes the cold-start window.
                try:
                    await filters.load()
                except Exception as exc:  # noqa: BLE001
                    log.warning("filter load failed at tick start: %s", exc)

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

                # 2b. Health-monitor emergency halt (app/trading/health.py) —
                # engaged automatically when a critical system check (exchange,
                # scheduler, database, duplicate/failed orders...) stays
                # unhealthy despite recovery attempts. Blocks new entries only;
                # existing positions keep being protected by risk gates above.
                try:
                    emergency_halted = bool((storage.kv_get("emergency_halt") or {}).get("active"))
                except Exception as exc:  # noqa: BLE001
                    log.warning("emergency halt flag check failed: %s", exc)
                    emergency_halted = False

                # 3. Agent signals → execute (skip BUYs if breaker tripped).
                try:
                    signals = await run_all_agents(use_llm=get_settings().llm_in_trading_loop)
                except Exception as exc:  # noqa: BLE001
                    self.state.last_error = f"agent run failed: {exc}"
                    log.exception("autopilot agent run failed")
                    self._save()
                    return
                try:
                    await self._execute(signals, allow_buys=not breaker_tripped and not emergency_halted)
                finally:
                    self._save()
            finally:
                storage.release_lock("autopilot_tick", owner=self._owner)

    # ── risk gates ─────────────────────────────────────────────────────
    async def _run_risk_gates(self) -> None:
        positions = [p for p in storage.all_positions() if p["mode"] == self.state.mode]
        if not positions:
            return
        # Actual free balances — risk exits must sell what the exchange really
        # holds, not the recorded position qty. A market BUY pays its fee out of
        # the received base asset (book 45 XLM, free 44.991), so selling the
        # book qty triggers a -2010 "insufficient balance" rejection on every
        # tick and a stop-loss/take-profit can never clear. Clamp to free.
        free_balances: dict[str, Decimal] = {}
        # Track whether the fetch actually succeeded. A missing key then means
        # "the exchange holds zero of this coin" (a zombie book position) rather
        # than "balance unknown" — the two require opposite handling below.
        balances_known = False
        try:
            snap = await portfolio_snapshot(mode=self.state.mode)
            balance_source = snap.get("free_balances") or snap.get("all_balances") or {}
            free_balances = {
                a: Decimal(str(q)) for a, q in balance_source.items()
            }
            balances_known = True
        except Exception as exc:  # noqa: BLE001
            log.warning("risk-gate balance fetch failed: %s", exc)
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
                base = ex.symbol.removesuffix("USDT")
                # When the balance fetch succeeded, a missing asset means the
                # exchange holds zero of it — a zombie book position. Use 0 so
                # the cleanup branch below closes it instead of submitting a
                # doomed full-qty SELL that Binance rejects with -2010 forever.
                # Only when the fetch FAILED (balances unknown) do we fall back
                # to the book qty.
                if balances_known:
                    avail = free_balances.get(base, Decimal("0"))
                else:
                    avail = None
                # Clamp the exit to the real free balance when we know it.
                sell_qty = ex.qty if avail is None else min(ex.qty, avail)
                qty = filters.round_qty(ex.symbol, sell_qty)
                if qty <= 0 or not filters.meets_min(ex.symbol, qty, price):
                    # Nothing sellable (dust below min-notional, or the balance
                    # is already gone). Close the stale book position so it stops
                    # re-triggering the gate every tick instead of erroring forever.
                    if avail is not None and avail < ex.qty:
                        log.warning(
                            "risk-exit %s: book qty=%s > free=%s and remainder "
                            "below min — closing stale position", ex.symbol, ex.qty, avail,
                        )
                        try:
                            storage.close_position(symbol=ex.symbol, exit_price=price)
                            risk.clear_hwm(ex.symbol)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("stale close failed for %s: %s", ex.symbol, exc)
                    else:
                        log.info("risk-exit %s skipped: filters reject qty=%s", ex.symbol, qty)
                    storage.record_tick_audit(
                        mode=self.state.mode,
                        symbol=ex.symbol,
                        timeframe="risk",
                        action="SELL",
                        score=100,
                        executed=False,
                        reason=f"risk_exit_skip:{ex.reason}",
                        indicators={
                            "entry_qty": str(ex.qty),
                            "free_qty": str(avail) if avail is not None else None,
                            "rounded_qty": str(qty),
                            "price": str(price),
                        },
                        filters={"meets_min": False},
                    )
                    continue
                log.warning("RISK EXIT %s reason=%s qty=%s price=%s (book=%s free=%s)",
                            ex.symbol, ex.reason, qty, price, ex.qty, avail)
                order = await self._submit(ex.symbol, OrderSide.SELL, qty, [f"risk:{ex.reason}"])
                storage.record_tick_audit(
                    mode=self.state.mode,
                    symbol=ex.symbol,
                    timeframe="risk",
                    action="SELL",
                    score=100,
                    executed=self._order_filled(order),
                    reason=f"risk_exit:{ex.reason}",
                    indicators={
                        "entry_qty": str(ex.qty),
                        "free_qty": str(avail) if avail is not None else None,
                        "rounded_qty": str(qty),
                        "price": str(price),
                    },
                    filters={"meets_min": True},
                )
                if self._order_filled(order):
                    risk.clear_hwm(ex.symbol)
                else:
                    log.error(
                        "RISK EXIT %s did NOT fill (status=%s) — position remains "
                        "open, will retry next tick", ex.symbol,
                        getattr(order, "status", None),
                    )
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
    async def _execute_signal(self, symbol: str, sig, *, allow_buys: bool) -> tuple[bool, str]:
        """Execute one aggregated signal through Validate -> Risk -> Execute -> Log."""
        s = get_settings()
        snap = await portfolio_snapshot(mode=self.state.mode)
        balance_source = snap.get("free_balances") or snap.get("all_balances") or {}
        balances: dict[str, Decimal] = {
            asset: Decimal(str(qty)) for asset, qty in balance_source.items()
        }
        usdt_free = Decimal(str(snap["usdt_cash"]))
        total_eq = Decimal(str(snap["total_usdt"]))
        open_positions = [
            p for p in storage.all_positions() if p["mode"] == self.state.mode
        ]
        position_exists = any(p["symbol"] == symbol for p in open_positions)

        if sig.action == SignalAction.HOLD:
            trade_audit_logger.log_event(
                mode=self.state.mode,
                symbol=symbol,
                signal=sig.action.value,
                confidence=float(sig.confidence),
                position_exists=position_exists,
                available_balance=usdt_free,
                final_outcome="rejected: hold",
                detail={"reason": "signal_hold"},
            )
            return False, "hold"

        if sig.confidence < s.min_signal_confidence:
            reason = "Confidence below threshold"
            trade_audit_logger.log_event(
                mode=self.state.mode,
                symbol=symbol,
                signal=sig.action.value,
                confidence=float(sig.confidence),
                position_exists=position_exists,
                available_balance=usdt_free,
                final_outcome=f"rejected: {reason}",
                detail={"threshold": s.min_signal_confidence},
            )
            return False, reason

        if sig.action == SignalAction.BUY and not allow_buys:
            reason = "Max exposure exceeded"
            trade_audit_logger.log_event(
                mode=self.state.mode,
                symbol=symbol,
                signal=sig.action.value,
                confidence=float(sig.confidence),
                risk_passed=False,
                position_exists=position_exists,
                available_balance=usdt_free,
                final_outcome=f"rejected: {reason}",
            )
            return False, reason

        if sig.action == SignalAction.BUY and position_exists:
            reason = "Position already exists"
            trade_audit_logger.log_event(
                mode=self.state.mode,
                symbol=symbol,
                signal=sig.action.value,
                confidence=float(sig.confidence),
                risk_passed=True,
                position_exists=True,
                available_balance=usdt_free,
                final_outcome=f"rejected: {reason}",
            )
            return False, reason

        if sig.action == SignalAction.SELL:
            base = symbol.removesuffix("USDT")
            free = balances.get(base, Decimal("0"))
            if free <= 0:
                reason = "SELL rejected: no holdings"
                trade_audit_logger.log_event(
                    mode=self.state.mode,
                    symbol=symbol,
                    signal=sig.action.value,
                    confidence=float(sig.confidence),
                    risk_passed=True,
                    position_exists=position_exists,
                    available_balance=free,
                    execution_attempted=False,
                    final_outcome=f"rejected: {reason}",
                )
                return False, reason
            order = await self._submit(symbol, OrderSide.SELL, free, list(getattr(sig, "contributing_agents", []) or []))
            ok = self._order_filled(order)
            trade_audit_logger.log_event(
                mode=self.state.mode,
                symbol=symbol,
                signal=sig.action.value,
                confidence=float(sig.confidence),
                risk_passed=True,
                position_exists=position_exists,
                available_balance=free,
                execution_attempted=True,
                min_notional_passed=True,
                binance_response=(getattr(order, "status", "NONE") if order else "NONE"),
                final_outcome=("executed" if ok else "rejected: Binance rejected order"),
                detail={"order_id": getattr(order, "exchange_order_id", None) if order else None},
            )
            return ok, ("executed" if ok else "Binance rejected order")

        # BUY path
        long_exposure_pct = float(
            (total_eq - usdt_free) / total_eq if total_eq > 0 else Decimal("0")
        )
        open_count = len(open_positions)
        ok_risk, why = risk.can_open_new_position(
            open_positions=open_count,
            long_exposure_pct=long_exposure_pct,
        )
        if not ok_risk:
            trade_audit_logger.log_event(
                mode=self.state.mode,
                symbol=symbol,
                signal=sig.action.value,
                confidence=float(sig.confidence),
                risk_passed=False,
                position_exists=position_exists,
                available_balance=usdt_free,
                final_outcome="rejected: Max exposure exceeded",
                detail={"reason": why},
            )
            return False, why

        atr_pct = await self._atr_pct(symbol)
        eff_pct = risk.volatility_scaled_pct(s.max_position_pct, atr_pct)
        per_trade_usdt = usdt_free * Decimal(str(eff_pct))
        if per_trade_usdt < 10:
            trade_audit_logger.log_event(
                mode=self.state.mode,
                symbol=symbol,
                signal=sig.action.value,
                confidence=float(sig.confidence),
                risk_passed=True,
                position_exists=position_exists,
                available_balance=usdt_free,
                min_notional_passed=False,
                final_outcome="rejected: No available funds",
                detail={"required_min_usdt": 10, "computed_usdt": str(per_trade_usdt)},
            )
            return False, "No available funds"

        plan = await self._buy_order_plan(symbol, per_trade_usdt)
        meets_min = bool(plan["meets_min"])
        if not meets_min:
            trade_audit_logger.log_event(
                mode=self.state.mode,
                symbol=symbol,
                signal=sig.action.value,
                confidence=float(sig.confidence),
                risk_passed=True,
                position_exists=position_exists,
                available_balance=usdt_free,
                min_notional_passed=False,
                final_outcome="rejected: Min notional check failed",
                detail={"plan": _jsonable(plan)},
            )
            return False, "Min notional check failed"

        order = await self._submit(
            symbol,
            OrderSide.BUY,
            plan["rounded_qty"],
            list(getattr(sig, "contributing_agents", []) or []),
        )
        ok = self._order_filled(order)
        trade_audit_logger.log_event(
            mode=self.state.mode,
            symbol=symbol,
            signal=sig.action.value,
            confidence=float(sig.confidence),
            risk_passed=True,
            position_exists=position_exists,
            available_balance=usdt_free,
            min_notional_passed=True,
            execution_attempted=True,
            binance_response=(getattr(order, "status", "NONE") if order else "NONE"),
            final_outcome=("executed" if ok else "rejected: Binance rejected order"),
            detail={"plan": _jsonable(plan), "order_id": getattr(order, "exchange_order_id", None) if order else None},
        )
        return ok, ("executed" if ok else "Binance rejected order")

    async def _execute(self, signals, *, allow_buys: bool = True) -> None:
        skip_counter: Counter[str] = Counter()
        tick_debug: dict[str, dict] = {}

        def _bump(reason: str, sym: str = "", detail: str = "") -> None:
            skip_counter[reason] += 1
            if sym:
                entry = tick_debug.setdefault(sym, {})
                entry["reason"] = reason
                entry["detail"] = detail

        def _entry(sym: str, sig=None) -> dict:
            entry = tick_debug.setdefault(sym, {})
            if sig is not None:
                entry.setdefault("action", getattr(sig.action, "value", str(sig.action)))
                entry.setdefault("confidence", float(sig.confidence))
                entry.setdefault(
                    "agents", list(getattr(sig, "contributing_agents", []) or [])
                )
            entry.setdefault("filters", {})
            return entry

        def _set_filter(sym: str, name: str, ok: bool, detail: str, sig=None) -> None:
            entry = _entry(sym, sig)
            entry["filters"][name] = {"ok": ok, "detail": detail}

        def _set_sizing(sym: str, payload: dict, sig=None) -> None:
            entry = _entry(sym, sig)
            entry["sizing"] = _jsonable(payload)

        def _finish(sym: str, reason: str, detail: str, *, submitted: bool, sig=None) -> None:
            entry = _entry(sym, sig)
            entry["final_reason"] = reason
            entry["submitted"] = submitted
            _bump(reason, sym, detail)
            min_notional_info = (entry.get("filters") or {}).get("min_notional") or {}
            min_notional_passed = min_notional_info.get("ok") if min_notional_info else None
            signal_val = entry.get("action") or (getattr(sig.action, "value", "HOLD") if sig is not None else "HOLD")
            confidence = entry.get("confidence")
            balances = (
                snap.get("free_balances") or snap.get("all_balances")
                if isinstance(snap, dict) else {}
            )
            avail = Decimal(str(snap.get("usdt_cash", "0"))) if isinstance(snap, dict) else Decimal("0")
            if signal_val == SignalAction.SELL.value:
                base = sym.removesuffix("USDT")
                avail = Decimal(str((balances or {}).get(base, 0)))
            trade_audit_logger.log_event(
                mode=self.state.mode,
                symbol=sym,
                signal=signal_val,
                confidence=float(confidence) if confidence is not None else None,
                risk_passed=(reason not in {"risk_cap", "breaker_tripped"}),
                position_exists=bool(sym in held_symbols),
                available_balance=avail,
                min_notional_passed=min_notional_passed,
                execution_attempted=submitted,
                binance_response=("SUCCESS" if submitted else "REJECTED"),
                exception=None,
                final_outcome=reason,
                detail={"detail": detail},
            )
            if entry.get("action") == SignalAction.BUY.value:
                log.info("[BUY_TRACE] %s %s", sym, json.dumps(_jsonable(entry), sort_keys=True))

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

        # Dynamic confidence bar — the online regime model leans the entry
        # threshold up (risk-off) or down (risk-on), bounded so it can't
        # override the technicals-based core. Computed once per tick.
        min_conf = s.min_signal_confidence
        if s.dynamic_threshold_enabled:
            delta, info = online_regime.threshold_delta()
            min_conf = max(0.0, min(1.0, s.min_signal_confidence + delta))
            log.info(
                "[REGIME] dynamic min_confidence=%.3f (base=%.2f) %s",
                min_conf, s.min_signal_confidence, info,
            )
        long_exposure_pct = float(
            (total_eq - usdt_free) / total_eq if total_eq > 0 else Decimal("0")
        )
        balance_source = snap.get("free_balances") or snap.get("all_balances") or {}
        balances: dict[str, Decimal] = {
            asset: Decimal(str(qty)) for asset, qty in balance_source.items()
        }
        open_positions = [
            p for p in storage.all_positions() if p["mode"] == self.state.mode
        ]
        open_count, held_symbols = await self._count_non_dust_positions(
            open_positions=open_positions,
            balances=balances,
        )
        now = datetime.now(timezone.utc)
        cooldown = timedelta(minutes=s.buy_cooldown_minutes)
        risk_manager = RiskManager()

        # ML quality gate — load the learned model once per tick. Trades whose
        # predicted win-probability is below the threshold are skipped. Loaded
        # lazily so a freshly retrained model is picked up next tick.
        ml_model = None
        ml_model_version: Optional[int] = None
        if s.ml_gate_enabled:
            try:
                artifact = storage.load_model_artifact("signal_quality_v1")
                if artifact:
                    ml_model = artifact["model"]
                    ml_model_version = artifact.get("version")
                    # Staleness guard: a model trained in a past market regime
                    # must not hold an indefinite veto. If it's older than the
                    # configured window, go advisory (fail-open) so entries
                    # aren't frozen forever while the learning loop retrains.
                    age_h = _model_age_hours(artifact.get("trained_at"))
                    if age_h is not None and age_h > s.ml_gate_max_model_age_hours:
                        if self._ml_logged_version != -2:
                            log.warning(
                                "[ML_GATE] model v%s is stale (%.1fh > %dh) — gate "
                                "is fail-open until a fresher model trains",
                                ml_model_version, age_h, s.ml_gate_max_model_age_hours,
                            )
                            self._ml_logged_version = -2
                        ml_model = None
                    # Log once per distinct version so retrains are visible
                    # without spamming every tick.
                    elif ml_model_version != self._ml_logged_version:
                        metrics = artifact.get("metrics") or {}
                        log.info(
                            "[ML_GATE] model loaded: version=%s algo=%s trained=%s "
                            "samples=%s auc=%.3f threshold=%.2f",
                            ml_model_version,
                            artifact.get("algorithm"),
                            artifact.get("trained_at"),
                            metrics.get("samples"),
                            float(metrics.get("roc_auc", 0.0)),
                            s.ml_gate_threshold,
                        )
                        self._ml_logged_version = ml_model_version
                else:
                    if self._ml_logged_version != -1:
                        log.warning(
                            "[ML_GATE] enabled but no trained model found "
                            "(signal_quality_v1) — gate is fail-open, all signals pass"
                        )
                        self._ml_logged_version = -1
            except Exception as exc:  # noqa: BLE001
                log.debug("ml gate model load failed: %s", exc)
                ml_model = None
        # Per-tick gate telemetry (exposed via /metrics).
        gate_evaluated = 0
        gate_accepted = 0
        gate_gated = 0
        gate_proba_sum = 0.0
        aggressive_mode, aggressive_reason = self._aggressive_mode_active()
        spread_cap = Decimal(
            str(
                getattr(s, "aggressive_max_spread_pct", 0.0025)
                if aggressive_mode else getattr(s, "rollback_max_spread_pct", getattr(s, "max_spread_pct", 0.0015))
            )
        )
        log.info("[AGGRESSIVE] %s spread_cap=%.4f%%", aggressive_reason, float(spread_cap) * 100)

        # Rank candidates by confidence (desc) so scarce cash and the
        # long-exposure cap are spent on the strongest signals first. Without
        # this, the loop consumed capital in the universe's order — which is
        # alphabetical — so it always filled the same early-alphabet coins and
        # starved stronger signals later in the list.
        ranked_signals = sorted(
            signals.items(), key=lambda kv: kv[1].confidence, reverse=True
        )
        for symbol, sig in ranked_signals:
            ml_proba: Optional[float] = None
            open_pos = next(
                (p for p in open_positions if p["symbol"] == symbol and p["mode"] == self.state.mode),
                None,
            )
            base_asset = symbol.removesuffix("USDT")
            free = balances.get(base_asset, Decimal("0"))
            # Short-circuit SELL intents only when we have neither a local
            # position row nor a real free balance on the exchange.
            if sig.action == SignalAction.SELL and open_pos is None and free <= 0:
                _bump(
                    "sell_no_position",
                    symbol,
                    f"no open position in {self.state.mode} mode",
                )
                continue
            # A SELL of a coin we actually hold is an EXIT, not an entry. Exits
            # must never be blocked by the entry-oriented gates below: the
            # dynamic confidence bar is leaned UP in risk-off (which would make
            # it harder to sell exactly when we most want out), and the ML
            # quality gate is calibrated on entry win-rates. If the ensemble
            # says SELL and we hold the asset, let the sale through — worst case
            # we sit in cash, the safe/reversible direction on spot.
            is_exit = (
                sig.action == SignalAction.SELL
                and free > 0
            )
            signal_min_conf = self._signal_min_confidence(sig.action)
            if sig.action == SignalAction.BUY:
                _set_filter(
                    symbol,
                    "signal_confidence",
                    sig.confidence >= signal_min_conf,
                    f"conf={sig.confidence:.3f} threshold={signal_min_conf:.3f}",
                    sig,
                )
            if sig.confidence < signal_min_conf and not is_exit:
                if sig.action == SignalAction.BUY:
                    _finish(
                        symbol,
                        "low_confidence",
                        f"{sig.action.value} conf={sig.confidence:.2f} < {signal_min_conf:.2f}",
                        submitted=False,
                        sig=sig,
                    )
                else:
                    _bump("low_confidence", symbol,
                          f"{sig.action.value} conf={sig.confidence:.2f} < {signal_min_conf:.2f}")
                log.info("[SIGNAL] SKIP %s %s conf=%.3f < %.2f (agents: %s)",
                         symbol, sig.action.value, sig.confidence, signal_min_conf,
                         ", ".join(sig.contributing_agents) or "none")
                continue
            current_price: Optional[Decimal] = None
            aggressive_exit_reason: Optional[str] = None
            if open_pos is not None:
                current_price = await self._price(symbol)
                entry_price = Decimal(str(open_pos.get("entry_price") or "0"))
                if entry_price > 0:
                    position_return_pct = (current_price - entry_price) / entry_price
                    hold_days: Optional[float]
                    hold_days = None
                    try:
                        entry_ts = datetime.fromisoformat(str(open_pos.get("entry_ts") or ""))
                        if entry_ts.tzinfo is None:
                            entry_ts = entry_ts.replace(tzinfo=timezone.utc)
                        hold_days = max(0.0, (now - entry_ts).total_seconds() / 86_400.0)
                    except Exception:  # noqa: BLE001
                        hold_days = None
                    aggressive_exit_reason = self._aggressive_exit_reason(
                        sig,
                        position_return_pct,
                        hold_days,
                    )
                    if aggressive_exit_reason is not None:
                        _set_filter(symbol, "aggressive_sell", True, aggressive_exit_reason, sig)
                        if free > 0:
                            placed = await self._place_sell(symbol, sig, free)
                            if placed:
                                skip_counter["executed_sell"] += 1
                                risk.clear_hwm(symbol)
                                self._clear_pyramid_adds_count(symbol)
                                _finish(symbol, "executed_sell", aggressive_exit_reason, submitted=True, sig=sig)
                            else:
                                _bump("filter_reject_sell", symbol)
                        else:
                            _bump("sell_no_balance", symbol)
                        continue
            if sig.action == SignalAction.HOLD:
                _bump("action_hold", symbol, f"conf={sig.confidence:.2f}")
                continue
            # Record the candidate signal for ML training BEFORE the quality gate.
            # The gate filters EXECUTION, but must never censor LEARNING: if we
            # only recorded gate-approved signals, a model biased against one
            # side (e.g. all BUYs in a downtrend) would starve itself of that
            # side's outcomes and could never relearn — a permanent one-way lock.
            # Recording every confidence-qualifying signal keeps the learning
            # loop fed with counterfactual outcomes so the gate can self-correct.
            if sig.action in (SignalAction.BUY, SignalAction.SELL):
                await self._record_signal_event(symbol, sig)
            if ml_model is not None and not is_exit and sig.action in (SignalAction.BUY, SignalAction.SELL):
                ml_proba = await self._ml_win_proba(ml_model, symbol, sig)
                if ml_proba is not None:
                    gate_threshold = self._ml_gate_threshold_for_confidence(
                        sig.confidence,
                        aggressive_mode,
                        sig.action,
                    )
                    gate_evaluated += 1
                    gate_proba_sum += ml_proba
                    if ml_proba < gate_threshold:
                        gate_gated += 1
                        log.info("[ML_GATE] SKIP %s %s proba=%.3f < %.2f",
                                 symbol, sig.action.value, ml_proba, gate_threshold)
                        if sig.action == SignalAction.BUY:
                            _set_filter(
                                symbol,
                                "ml_gate",
                                False,
                                f"proba={ml_proba:.3f} threshold={gate_threshold:.3f}",
                                sig,
                            )
                            _finish(
                                symbol,
                                "ml_gate",
                                f"{sig.action.value} proba={ml_proba:.2f} < {gate_threshold}",
                                submitted=False,
                                sig=sig,
                            )
                        else:
                            _bump("ml_gate", symbol,
                                  f"{sig.action.value} proba={ml_proba:.2f} < {gate_threshold}")
                        continue
                    gate_accepted += 1
                    log.info("[ML_GATE] PASS %s %s proba=%.3f >= %.2f",
                             symbol, sig.action.value, ml_proba, gate_threshold)
                    if sig.action == SignalAction.BUY:
                        _set_filter(
                            symbol,
                            "ml_gate",
                            True,
                            f"proba={ml_proba:.3f} threshold={gate_threshold:.3f}",
                            sig,
                        )
            if not filters.is_listed(symbol):
                if sig.action == SignalAction.BUY:
                    _set_filter(symbol, "listed", False, "symbol not trading on Binance.US", sig)
                    _finish(symbol, "not_listed", "symbol not trading on Binance.US", submitted=False, sig=sig)
                else:
                    _bump("not_listed", symbol)
                continue
            if sig.action == SignalAction.BUY:
                _set_filter(symbol, "listed", True, "symbol trading on Binance.US", sig)
            try:
                if sig.action == SignalAction.BUY:
                    pyramid_adds = self._pyramid_adds_count(symbol)
                    pyramid_threshold = 0.75
                    max_pyramid_adds = 2
                    is_pyramid = bool(
                        open_pos
                        and aggressive_mode
                        and sig.confidence >= pyramid_threshold
                        and pyramid_adds < max_pyramid_adds
                    )
                    if not allow_buys:
                        _set_filter(symbol, "drawdown_breaker", False, "new BUYs halted by circuit breaker", sig)
                        _finish(symbol, "breaker_tripped", "new BUYs halted by circuit breaker", submitted=False, sig=sig)
                        continue
                    _set_filter(symbol, "drawdown_breaker", True, "circuit breaker allows BUY", sig)
                    if symbol in held_symbols and not is_pyramid:
                        reason = "existing position already held"
                        if open_pos and aggressive_mode and sig.confidence >= pyramid_threshold and pyramid_adds >= max_pyramid_adds:
                            reason = f"pyramid limit reached ({pyramid_adds}/{max_pyramid_adds})"
                        _set_filter(symbol, "already_held", False, reason, sig)
                        _finish(symbol, "already_held", reason, submitted=False, sig=sig)
                        continue  # don't pyramid into existing position
                    if is_pyramid:
                        _set_filter(
                            symbol,
                            "already_held",
                            True,
                            (
                                f"pyramiding allowed conf={sig.confidence:.3f} >= {pyramid_threshold:.3f} "
                                f"add={pyramid_adds + 1}/{max_pyramid_adds}"
                            ),
                            sig,
                        )
                    else:
                        _set_filter(symbol, "already_held", True, "position not currently held", sig)
                    if self._on_cooldown(symbol, now, cooldown) and not is_pyramid:
                        _set_filter(symbol, "cooldown", False, f"cooldown={cooldown}", sig)
                        _finish(symbol, "cooldown", f"cooldown={cooldown}", submitted=False, sig=sig)
                        continue
                    _set_filter(
                        symbol,
                        "cooldown",
                        True,
                        ("cooldown bypassed for pyramid add" if is_pyramid else f"cooldown window clear ({cooldown})"),
                        sig,
                    )
                    entry_price = current_price if current_price is not None else await self._price(symbol)
                    current_position_notional = None
                    if open_pos is not None:
                        current_position_notional = Decimal(str(open_pos.get("qty") or "0")) * entry_price
                    entry_risk = risk_manager.evaluate_entry(
                        mode=self.state.mode,
                        total_equity_usdt=total_eq,
                        open_positions=open_count,
                        long_exposure_pct=long_exposure_pct,
                        entry_price=entry_price,
                        aggressive_mode=aggressive_mode,
                        is_pyramid=is_pyramid,
                        current_position_notional=current_position_notional,
                    )
                    _set_filter(symbol, "risk_manager", entry_risk.allow, entry_risk.reason, sig)
                    if not entry_risk.allow:
                        _finish(symbol, "risk_manager", entry_risk.reason, submitted=False, sig=sig)
                        log.info("skip %s BUY: %s", symbol, entry_risk.reason)
                        continue

                    atr_pct = await self._atr_pct(symbol)
                    per_trade_usdt = entry_risk.notional_usdt
                    eff_pct = float(per_trade_usdt / total_eq) if total_eq > 0 else 0.0
                    buy_plan = await self._buy_order_plan(symbol, per_trade_usdt)
                    buy_plan["atr_pct"] = atr_pct
                    buy_plan["effective_position_pct"] = eff_pct
                    buy_plan["usdt_free"] = usdt_free
                    _set_sizing(symbol, buy_plan, sig)
                    _set_filter(
                        symbol,
                        "min_notional",
                        bool(buy_plan["meets_min"]),
                        (
                            f"qty={buy_plan['rounded_qty']} notional={buy_plan['notional']} "
                            f"min_qty={buy_plan['min_qty']} min_notional={buy_plan['min_notional']}"
                        ),
                        sig,
                    )

                    # Market-regime kill-switch — block ALL new longs while the
                    # broad market (BTC) is in a confirmed downtrend. Spot is
                    # long-only; walk-forward backtests show every sustained loss
                    # happens in BTC bear regimes, so sit in cash instead.
                    market_ok, market_why = await self._market_gate()
                    _set_filter(symbol, "market_regime", market_ok, market_why, sig)
                    if not market_ok:
                        _finish(symbol, "market_gate", market_why, submitted=False, sig=sig)
                        log.info("skip %s BUY: %s", symbol, market_why)
                        continue

                    # Long-term trend filter — don't buy an asset below its
                    # 200-EMA. Spot is long-only, so a downtrend long just feeds
                    # the stop-loss gate. Backtest-validated structural guard.
                    trend_ok, trend_why = await self._trend_gate(symbol)
                    if not trend_ok and self._trend_gate_bypass_allowed(sig.confidence, ml_proba, aggressive_mode):
                        trend_ok = True
                        trend_why = f"bypassed conf={sig.confidence:.3f} ml_proba={ml_proba:.3f}"
                    _set_filter(symbol, "trend_gate", trend_ok, trend_why, sig)
                    if not trend_ok:
                        _finish(symbol, "trend_gate", trend_why, submitted=False, sig=sig)
                        log.info("skip %s BUY: %s", symbol, trend_why)
                        continue
                    # Enforce $10 minimum per trade
                    if per_trade_usdt < 10:
                        if usdt_free >= 10:
                            per_trade_usdt = Decimal("10")
                        else:
                            _set_filter(
                                symbol,
                                "cash_available",
                                False,
                                f"per_trade={per_trade_usdt:.4f} cash={usdt_free:.2f} eff={eff_pct:.4f}",
                                sig,
                            )
                            _finish(
                                symbol,
                                "insufficient_usdt",
                                f"per_trade={per_trade_usdt:.4f} cash={usdt_free:.2f} eff={eff_pct:.4f}",
                                submitted=False,
                                sig=sig,
                            )
                            continue
                    _set_filter(symbol, "cash_available", True, f"usdt_free={usdt_free}", sig)

                    # Derivatives context gate (funding too negative → skip long).
                    fund_ok, fund_why = await self._funding_gate(symbol)
                    _set_filter(symbol, "funding_gate", fund_ok, fund_why, sig)
                    if not fund_ok:
                        _finish(symbol, "funding_gate", fund_why, submitted=False, sig=sig)
                        log.info("skip %s BUY: %s", symbol, fund_why)
                        continue

                    # On-chain whale-flow gate (exchange inflow spike → skip long).
                    flow_ok, flow_why = await self._onchain_gate(symbol)
                    _set_filter(symbol, "onchain_gate", flow_ok, flow_why, sig)
                    if not flow_ok:
                        _finish(symbol, "onchain_gate", flow_why, submitted=False, sig=sig)
                        log.info("skip %s BUY: %s", symbol, flow_why)
                        continue

                    # Order-book liquidity gate (reject thin/wide books).
                    ob_ok, ob_why = await liquidity_gate(
                        symbol, SignalAction.BUY, per_trade_usdt, max_spread_pct=spread_cap
                    )
                    _set_filter(symbol, "orderbook_gate", ob_ok, ob_why, sig)
                    if not ob_ok:
                        retry_scheduled = False
                        if aggressive_mode and getattr(s, "orderbook_retry_enabled", True):
                            task_key = (symbol, sig.action.value)
                            if task_key not in self._orderbook_retry_tasks:
                                self._orderbook_retry_tasks[task_key] = asyncio.create_task(
                                    self._retry_orderbook_recheck(
                                        symbol=symbol,
                                        sig=sig,
                                        per_trade_usdt=per_trade_usdt,
                                        attempt=1,
                                        aggressive_mode=aggressive_mode,
                                        spread_cap=spread_cap,
                                    )
                                )
                                retry_scheduled = True
                        detail = ob_why if not retry_scheduled else f"{ob_why}; retry_scheduled=60s x{int(getattr(s, 'orderbook_retry_attempts', 3))}"
                        _finish(symbol, "orderbook_gate", detail, submitted=False, sig=sig)
                        log.info("skip %s BUY: order book %s", symbol, ob_why)
                        continue

                    placed = await self._place_buy(symbol, sig, per_trade_usdt)
                    if not placed:
                        _finish(symbol, "filter_reject_buy", "exchange filters rejected computed qty", submitted=False, sig=sig)
                        continue
                    self.state.cooldowns[symbol] = now.isoformat()
                    if not is_pyramid:
                        open_count += 1
                        held_symbols.add(symbol)
                        self._clear_pyramid_adds_count(symbol)
                    else:
                        self._set_pyramid_adds_count(symbol, pyramid_adds + 1)
                    skip_counter["executed_buy"] += 1
                    _finish(
                        symbol,
                        ("executed_pyramid_buy" if is_pyramid else "executed_buy"),
                        (
                            f"submitted qty={buy_plan['rounded_qty']} notional={buy_plan['notional']} "
                            f"price={buy_plan['price']}"
                        ),
                        submitted=True,
                        sig=sig,
                    )
                    # Approximate exposure update so subsequent BUYs see the new total.
                    long_exposure_pct = min(
                        1.0,
                        long_exposure_pct + float(per_trade_usdt / total_eq) if total_eq > 0 else 0,
                    )
                elif sig.action == SignalAction.SELL:
                    # SAFETY: Check if an open position actually exists
                    if free > 0:
                        placed = await self._place_sell(symbol, sig, free)
                        if placed:
                            skip_counter["executed_sell"] += 1
                            risk.clear_hwm(symbol)
                            self._clear_pyramid_adds_count(symbol)
                        else:
                            _bump("filter_reject_sell", symbol)
                    else:
                        if not open_pos:
                            _bump("sell_no_position", symbol,
                                  f"no open position in {self.state.mode} mode")
                        else:
                            _bump("sell_no_balance", symbol)
            except Exception as exc:  # noqa: BLE001
                self.state.last_error = f"{symbol}: {exc}"
                log.warning("execute failed %s: %s", symbol, exc)
                _bump("exception", symbol, str(exc))
                # Record this to trade_audit too (not just the in-memory skip
                # counter) — otherwise a real exchange/network exception during
                # order placement (as opposed to a clean "Binance rejected the
                # order" response) leaves no row for health.py's
                # _check_failed_orders() to find, and a persistent outage could
                # go undetected by the watchdog even though it's failing every
                # tick. Never let a failure recording itself throw.
                try:
                    trade_audit_logger.log_event(
                        mode=self.state.mode,
                        symbol=symbol,
                        signal=getattr(sig.action, "value", str(sig.action)),
                        confidence=float(sig.confidence),
                        position_exists=bool(symbol in held_symbols),
                        execution_attempted=True,
                        binance_response="EXCEPTION",
                        exception=str(exc),
                        final_outcome=f"rejected: exception: {exc}",
                        detail={"detail": str(exc)},
                    )
                except Exception:  # noqa: BLE001
                    log.debug(
                        "trade_audit record-of-exception itself failed for %s",
                        symbol, exc_info=True,
                    )

        if self.state.mode == "live":
            await self._log_live_held_positions(now)

        self._persist_skip_stats(skip_counter, tick_debug, total=len(signals))
        if s.ml_gate_enabled:
            self._persist_gate_stats(
                evaluated=gate_evaluated,
                accepted=gate_accepted,
                gated=gate_gated,
                proba_sum=gate_proba_sum,
                threshold=s.ml_gate_threshold,
                model_version=ml_model_version,
            )

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

    def _persist_gate_stats(
        self,
        *,
        evaluated: int,
        accepted: int,
        gated: int,
        proba_sum: float,
        threshold: float,
        model_version: Optional[int],
    ) -> None:
        """Accumulate ML-gate telemetry for the /metrics endpoint."""
        try:
            prev = storage.kv_get(_ML_GATE_STATS_KEY) or {}
            cum = prev.get("cumulative", {}) if isinstance(prev, dict) else {}
            cum = {
                "evaluated": int(cum.get("evaluated", 0)) + evaluated,
                "accepted": int(cum.get("accepted", 0)) + accepted,
                "gated": int(cum.get("gated", 0)) + gated,
                "proba_sum": float(cum.get("proba_sum", 0.0)) + proba_sum,
            }
            last = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "evaluated": evaluated,
                "accepted": accepted,
                "gated": gated,
                "avg_proba": (proba_sum / evaluated) if evaluated else None,
            }
            storage.kv_set(_ML_GATE_STATS_KEY, {
                "threshold": threshold,
                "model_version": model_version,
                "cumulative": cum,
                "last_tick": last,
            })
        except Exception as exc:  # noqa: BLE001
            log.debug("gate-stats persist failed: %s", exc)

    async def _log_live_held_positions(self, now: datetime) -> None:
        """Emit per-position exit readiness diagnostics for live mode."""
        live_positions = [
            p for p in storage.all_positions() if p.get("mode") == "live"
        ]
        for pos in live_positions:
            symbol = str(pos.get("symbol") or "")
            entry_raw = pos.get("entry_price")
            if not symbol or entry_raw is None:
                continue
            try:
                entry_price = Decimal(str(entry_raw))
                if entry_price <= 0:
                    continue
                current_price = await self._price(symbol)
            except Exception as exc:  # noqa: BLE001
                log.debug("[HELD] %s price/entry unavailable: %s", symbol, exc)
                continue

            try:
                entry_ts = datetime.fromisoformat(str(pos.get("entry_ts") or ""))
                if entry_ts.tzinfo is None:
                    entry_ts = entry_ts.replace(tzinfo=timezone.utc)
                hold_days = max(0.0, (now - entry_ts).total_seconds() / 86_400.0)
            except Exception:  # noqa: BLE001
                hold_days = 0.0

            pnl_pct = ((current_price - entry_price) / entry_price) * Decimal("100")
            exit_ready = (
                pnl_pct >= Decimal("2.0")
                or pnl_pct <= Decimal("-5.0")
                or hold_days >= 7.0
            )
            log.info(
                "[HELD] %s PnL=%.2f%% days=%.2f exit_ready=%s",
                symbol,
                float(pnl_pct),
                hold_days,
                exit_ready,
            )

    def _on_cooldown(self, symbol: str, now: datetime, cooldown: timedelta) -> bool:
        ts = self.state.cooldowns.get(symbol)
        if not ts:
            return False
        last = _parse_dt(ts)
        if not last:
            return False
        return (now - last) < cooldown

    def _pyramid_adds_key(self, symbol: str) -> str:
        return f"pyramid_adds:{self.state.mode}:{symbol}"

    def _pyramid_adds_count(self, symbol: str) -> int:
        return int(storage.kv_get(self._pyramid_adds_key(symbol), 0) or 0)

    def _set_pyramid_adds_count(self, symbol: str, count: int) -> None:
        storage.kv_set(self._pyramid_adds_key(symbol), max(0, int(count)))

    def _clear_pyramid_adds_count(self, symbol: str) -> None:
        self._set_pyramid_adds_count(symbol, 0)

    def _aggressive_mode_active(self) -> tuple[bool, str]:
        s = get_settings()
        if not getattr(s, "aggressive_mode_enabled", True):
            return False, "disabled"
        min_trades = int(getattr(s, "aggressive_rollback_min_trades", 30))
        min_win_rate = float(getattr(s, "aggressive_rollback_min_win_rate", 0.50))
        trades = [
            t for t in storage.closed_trades(limit=min_trades)
            if t.get("mode") == self.state.mode
        ]
        if len(trades) < min_trades:
            return True, f"warmup:{len(trades)}/{min_trades}"
        wins = sum(1 for t in trades if Decimal(str(t.get("pnl", 0))) > 0)
        win_rate = wins / len(trades) if trades else 0.0
        if win_rate < min_win_rate:
            return False, f"rollback:win_rate={win_rate:.2f}<{min_win_rate:.2f}"
        return True, f"active:win_rate={win_rate:.2f}"

    def _ml_gate_threshold_for_confidence(
        self,
        confidence: float,
        aggressive_mode: bool,
        action: SignalAction | str | None = None,
    ) -> float:
        del aggressive_mode
        s = get_settings()
        action_value = getattr(action, "value", action)
        if action_value == SignalAction.BUY.value:
            if confidence >= 0.90:
                return float(getattr(s, "ml_gate_threshold_conf_90", 0.35))
            if confidence >= 0.80:
                return float(getattr(s, "ml_gate_threshold_conf_80", 0.40))
            if confidence >= 0.70:
                return float(getattr(s, "ml_gate_threshold_conf_70", 0.45))
            return float(getattr(s, "ml_gate_threshold", 0.50))
        if action_value == SignalAction.SELL.value:
            return float(getattr(s, "ml_gate_threshold", 0.50))
        return float(getattr(s, "ml_gate_threshold", 0.50))

    def _signal_min_confidence(self, action: SignalAction | str) -> float:
        action_value = getattr(action, "value", action)
        if action_value == SignalAction.BUY.value:
            return 0.40
        if action_value == SignalAction.SELL.value:
            return 0.497
        return 0.497

    def _aggressive_exit_reason(
        self,
        sig,
        position_return_pct: Decimal,
        hold_days: Optional[float],
    ) -> Optional[str]:
        if sig.action == SignalAction.SELL and sig.confidence > 0.60:
            return f"sell confidence={sig.confidence:.2f} > 0.60"
        if position_return_pct >= Decimal("0.02"):
            return f"profit={float(position_return_pct) * 100:.2f}% >= 2.00%"
        if position_return_pct <= Decimal("-0.05"):
            return f"loss={float(position_return_pct) * 100:.2f}% <= -5.00%"
        if hold_days is not None and hold_days > 7.0:
            return f"hold_days={hold_days:.2f} > 7.00"
        return None

    def _trend_gate_bypass_allowed(
        self,
        confidence: float,
        ml_proba: Optional[float],
        aggressive_mode: bool,
    ) -> bool:
        s = get_settings()
        if not aggressive_mode or ml_proba is None:
            return False
        return (
            confidence > float(getattr(s, "trend_gate_bypass_confidence", 0.85))
            and ml_proba > float(getattr(s, "trend_gate_bypass_ml_proba", 0.55))
        )

    async def _retry_orderbook_recheck(
        self,
        *,
        symbol: str,
        sig,
        per_trade_usdt: Decimal,
        attempt: int,
        aggressive_mode: bool,
        spread_cap: Decimal,
    ) -> None:
        s = get_settings()
        task_key = (symbol, sig.action.value)
        try:
            await asyncio.sleep(int(getattr(s, "orderbook_retry_delay_seconds", 60)))
            if not self.state.running or sig.action != SignalAction.BUY:
                return

            snap = await portfolio_snapshot(mode=self.state.mode)
            balance_source = snap.get("free_balances") or snap.get("all_balances") or {}
            balances: dict[str, Decimal] = {
                asset: Decimal(str(qty)) for asset, qty in balance_source.items()
            }
            open_positions = [
                p for p in storage.all_positions() if p["mode"] == self.state.mode
            ]
            open_count, held_symbols = await self._count_non_dust_positions(
                open_positions=open_positions,
                balances=balances,
            )
            if symbol in held_symbols:
                return

            usdt_free = Decimal(str(snap["usdt_cash"]))
            total_eq = Decimal(str(snap["total_usdt"]))
            long_exposure_pct = float(
                (total_eq - usdt_free) / total_eq if total_eq > 0 else Decimal("0")
            )
            entry_price = await self._price(symbol)
            entry_risk = RiskManager().evaluate_entry(
                mode=self.state.mode,
                total_equity_usdt=total_eq,
                open_positions=open_count,
                long_exposure_pct=long_exposure_pct,
                entry_price=entry_price,
                aggressive_mode=aggressive_mode,
            )
            if not entry_risk.allow:
                log.info("[OB_RETRY] %s attempt=%d skipped: %s", symbol, attempt, entry_risk.reason)
                return

            retry_notional = min(per_trade_usdt, usdt_free, entry_risk.notional_usdt)
            if retry_notional <= 0:
                return
            ob_ok, ob_why = await liquidity_gate(
                symbol,
                SignalAction.BUY,
                retry_notional,
                max_spread_pct=spread_cap,
            )
            if not ob_ok:
                log.info("[OB_RETRY] %s attempt=%d blocked: %s", symbol, attempt, ob_why)
                if getattr(s, "orderbook_retry_enabled", True) and attempt < int(getattr(s, "orderbook_retry_attempts", 3)):
                    next_task = asyncio.create_task(
                        self._retry_orderbook_recheck(
                            symbol=symbol,
                            sig=sig,
                            per_trade_usdt=retry_notional,
                            attempt=attempt + 1,
                            aggressive_mode=aggressive_mode,
                            spread_cap=spread_cap,
                        )
                    )
                    self._orderbook_retry_tasks[task_key] = next_task
                return

            placed = await self._place_buy(symbol, sig, retry_notional)
            if placed:
                self.state.cooldowns[symbol] = datetime.now(timezone.utc).isoformat()
                self._save()
                log.info("[OB_RETRY] %s attempt=%d executed after recheck", symbol, attempt)
            else:
                log.info("[OB_RETRY] %s attempt=%d failed at submit stage", symbol, attempt)
        except Exception as exc:  # noqa: BLE001
            log.warning("[OB_RETRY] %s attempt=%d failed: %s", symbol, attempt, exc)
        finally:
            task = self._orderbook_retry_tasks.get(task_key)
            if task is asyncio.current_task():
                self._orderbook_retry_tasks.pop(task_key, None)

    async def _count_non_dust_positions(
        self,
        *,
        open_positions: list[dict],
        balances: dict[str, Decimal],
    ) -> tuple[int, set[str]]:
        """Return (count, symbols) of open positions that are not dust.

        Dust (below LOT_SIZE/MIN_NOTIONAL) should not consume a position slot.
        """
        non_dust_symbols: set[str] = set()
        price_cache: dict[str, Decimal] = {}

        for pos in open_positions:
            symbol = str(pos.get("symbol") or "")
            if not symbol:
                continue
            base = symbol.removesuffix("USDT")
            book_qty = Decimal(str(pos.get("qty") or "0"))
            qty = balances.get(base, book_qty)
            if qty <= 0:
                continue

            if symbol not in price_cache:
                try:
                    price_cache[symbol] = await self._price(symbol)
                except Exception as exc:  # noqa: BLE001
                    log.debug("non-dust price fetch failed for %s: %s", symbol, exc)
                    try:
                        entry = Decimal(str(pos.get("entry_price") or "0"))
                    except Exception as e:  # noqa: BLE001
                        log.exception("Trade execution failure: %s", e)
                        entry = Decimal("0")
                    if entry <= 0:
                        continue
                    price_cache[symbol] = entry

            price = price_cache[symbol]
            rounded = filters.round_qty(symbol, qty)
            if rounded <= 0:
                continue
            if not filters.meets_min(symbol, rounded, price):
                continue
            non_dust_symbols.add(symbol)

        return len(non_dust_symbols), non_dust_symbols

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
        # Prefer the live websocket last-price when it's fresh; this avoids
        # pricing fills against a candle close that can be up to ~15 min old.
        # Falls back to REST (paper uses the public ticker) on any miss.
        if get_settings().live_price_enabled:
            live = live_prices.get_fresh(symbol)
            if live is not None and live > 0:
                return live
        if self.state.mode == "paper":
            return await paper_exchange.ticker_price(symbol)
        return await BinanceUSClient().ticker_price(symbol)

    async def _funding_gate(self, symbol: str) -> tuple[bool, str]:
        """Veto new longs when perp funding is deeply negative (crowded short).

        Also records OI-vs-price trend confirmation (informational, non-blocking).
        FAIL-OPEN: disabled or unavailable derivatives data always allows the trade.
        """
        s = get_settings()
        if not s.derivatives_data_enabled:
            return True, "deriv_disabled"
        try:
            ctx = await derivatives.context(symbol)
        except Exception as exc:  # noqa: BLE001
            log.debug("[DERIV] %s context failed (%s) — allowing", symbol, exc)
            return True, f"deriv_unavailable:{exc}"
        if ctx is None:
            return True, "deriv_none"

        # OI trend confirmation: rising OI + rising price = fresh-money trend.
        try:
            await self._log_oi_trend(symbol, ctx)
        except Exception as exc:  # noqa: BLE001
            log.debug("[DERIV] %s OI trend log failed: %s", symbol, exc)

        if ctx.funding_rate is not None and ctx.funding_rate < s.funding_min_pct:
            return False, (
                f"funding {ctx.funding_rate:.4%} < {s.funding_min_pct:.4%} (crowded short)"
            )
        return True, f"funding={ctx.funding_rate}"

    async def _log_oi_trend(self, symbol: str, ctx) -> None:
        """Compare current OI/price to the last snapshot and log trend confirmation."""
        if ctx.open_interest is None:
            return
        key = "deriv_oi_last"
        store = storage.kv_get(key) or {}
        prev = store.get(symbol) if isinstance(store, dict) else None
        try:
            price_now = float(await self._price(symbol))
        except Exception as e:  # noqa: BLE001
            log.exception("Trade execution failure: %s", e)
            price_now = None
        if prev and price_now is not None:
            d_oi = ctx.open_interest - float(prev.get("oi", 0.0))
            d_px = price_now - float(prev.get("price", 0.0))
            if d_oi > 0 and d_px > 0:
                log.info("[DERIV] %s trend CONFIRM: OI+ price+ (fresh money long)", symbol)
            elif d_oi < 0 and d_px > 0:
                log.info("[DERIV] %s price+ on OI- (short-covering, weaker)", symbol)
        if isinstance(store, dict):
            store[symbol] = {"oi": ctx.open_interest, "price": price_now or 0.0}
            storage.kv_set(key, store)

    async def _trend_gate(self, symbol: str) -> tuple[bool, str]:
        """Veto new longs when price is below its long-term trend (200-EMA).

        Binance.US spot is long-only, so buying an asset that is trending down
        only feeds the stop-loss/take-profit gate and churns fees. Require the
        latest daily close to be at or above the 200-EMA.
        FAIL-OPEN: disabled or missing data always allows the trade.
        """
        s = get_settings()
        if not s.trend_filter_enabled:
            return True, "trend_disabled"
        try:
            from app.data import OHLCVRepository
            from app.ta import add_indicators

            df = await OHLCVRepository().get(symbol, Timeframe.D1, refresh=False)
            df = add_indicators(df).dropna()
            if df.empty or "ema_200" not in df.columns:
                return True, "trend_no_data"
            last = df.iloc[-1]
            close = float(last["close"])
            ema200 = float(last["ema_200"])
            if ema200 <= 0:
                return True, "trend_no_data"
            if close < ema200:
                return False, f"close {close:.6g} < ema200 {ema200:.6g} (downtrend)"
            return True, f"close>={ema200:.6g}"
        except Exception as exc:  # noqa: BLE001
            log.debug("[TREND] %s gate failed (%s) — allowing", symbol, exc)
            return True, f"trend_unavailable:{exc}"

    async def _market_gate(self) -> tuple[bool, str]:
        """Portfolio-wide kill-switch: block new longs in a BTC downtrend.

        Risk-OFF when BTC's 50-EMA is below its 200-EMA (a confirmed "death
        cross"). Walk-forward backtests show every sustained loss occurs while
        the broad market bleeds; spot is long-only so there is no edge to take
        there — stay in cash. The verdict is identical for every symbol in a
        tick, so it is cached briefly to avoid refetching BTC per candidate.
        FAIL-OPEN: disabled or missing BTC data always allows trading.
        """
        s = get_settings()
        if not getattr(s, "market_regime_gate_enabled", True):
            return True, "market_gate_disabled"
        cache = self._market_regime_cache
        if cache is not None and (asyncio.get_event_loop().time() - cache[2]) < 300.0:
            return cache[0], cache[1]
        allowed, reason = True, "market_no_data"
        try:
            from app.data import OHLCVRepository
            from app.ta import add_indicators

            df = await OHLCVRepository().get("BTCUSDT", Timeframe.D1, refresh=False)
            df = add_indicators(df).dropna()
            if not df.empty and {"ema_50", "ema_200"} <= set(df.columns):
                last = df.iloc[-1]
                ema50 = float(last["ema_50"])
                ema200 = float(last["ema_200"])
                if ema200 > 0:
                    if ema50 < ema200:
                        allowed = False
                        reason = f"BTC risk-off (ema50 {ema50:.0f} < ema200 {ema200:.0f})"
                    else:
                        allowed = True
                        reason = f"BTC risk-on (ema50 {ema50:.0f} >= ema200 {ema200:.0f})"
        except Exception as exc:  # noqa: BLE001
            log.debug("[MARKET] regime gate failed (%s) — allowing", exc)
            reason = f"market_unavailable:{exc}"
        self._market_regime_cache = (allowed, reason, asyncio.get_event_loop().time())
        return allowed, reason

    async def _onchain_gate(self, symbol: str) -> tuple[bool, str]:
        """Veto new longs on an exchange-inflow spike (coins moving in to be sold).

        FAIL-OPEN: disabled, no key, or unavailable on-chain data allows the trade.
        """
        s = get_settings()
        if not s.onchain_enabled:
            return True, "onchain_disabled"
        try:
            from app.data.onchain import inflow_spike

            spiked, detail = await inflow_spike(symbol)
        except Exception as exc:  # noqa: BLE001
            log.debug("[ONCHAIN] %s check failed (%s) — allowing", symbol, exc)
            return True, f"onchain_unavailable:{exc}"
        if spiked:
            return False, f"exchange inflow spike ({detail})"
        return True, detail

    async def _place_buy(self, symbol: str, sig, per_trade_usdt: Decimal) -> bool:
        plan = await self._buy_order_plan(symbol, per_trade_usdt)
        price = plan["price"]
        qty = plan["rounded_qty"]
        if qty <= 0 or not plan["meets_min"]:
            log.info("skip %s BUY: filters reject qty=%s price=%s", symbol, qty, price)
            return False
        agents = list(getattr(sig, "contributing_agents", []) or [])
        order = await self._submit(symbol, OrderSide.BUY, qty, agents)
        return self._order_filled(order)

    async def _buy_order_plan(self, symbol: str, per_trade_usdt: Decimal) -> dict[str, Decimal | bool | None]:
        price = await self._price(symbol)
        raw_qty = (per_trade_usdt / price) if price > 0 else Decimal("0")
        rounded_qty = filters.round_qty(symbol, raw_qty)
        min_check = filters.diagnostics(symbol, rounded_qty, price)
        notional = rounded_qty * price
        # Hard $10 entry floor (config.min_trade_usdt), on top of whatever the
        # exchange's own MIN_NOTIONAL filter allows. Entries only — sells are
        # never gated by this so risk exits can always fully liquidate.
        min_trade_floor = Decimal(str(get_settings().min_trade_usdt))
        exchange_min_notional = min_check.get("min_notional")
        effective_min_notional = max(min_trade_floor, exchange_min_notional or Decimal("0"))
        notional_ok = bool(min_check.get("notional_ok")) and notional >= min_trade_floor
        return {
            "price": price,
            "per_trade_usdt": per_trade_usdt,
            "raw_qty": raw_qty,
            "rounded_qty": rounded_qty,
            "notional": notional,
            "min_qty": min_check.get("min_qty"),
            "min_notional": effective_min_notional,
            "meets_min": bool(min_check.get("qty_ok")) and notional_ok,
            "qty_ok": bool(min_check.get("qty_ok")),
            "notional_ok": notional_ok,
        }

    async def _place_sell(self, symbol: str, sig, free: Decimal) -> bool:
        price = await self._price(symbol)
        qty = filters.round_qty(symbol, free)
        diag = filters.diagnostics(symbol, qty, price)
        min_qty = diag.get("min_qty")
        min_notional = diag.get("min_notional")

        # Dust/dilution handling: try a minimum-qty sell when the position
        # value is large enough, otherwise reject with explicit diagnostics.
        qty_floor_reject = qty <= 0 or (min_qty is not None and qty < min_qty)
        if qty_floor_reject:
            position_value = free * price
            if (
                min_qty is not None
                and min_notional is not None
                and position_value >= min_notional
                and free >= min_qty
            ):
                qty = filters.round_qty(symbol, min_qty)
                diag = filters.diagnostics(symbol, qty, price)
                log.info(
                    "sell %s dust adjust: free=%s rounded=%s -> min_qty=%s notional=%s",
                    symbol,
                    free,
                    filters.round_qty(symbol, free),
                    qty,
                    qty * price,
                )
            else:
                log.info(
                    "skip %s SELL: filter_reject_sell: dust balance %s < min_qty %s "
                    "(value=%s min_notional=%s)",
                    symbol,
                    qty,
                    min_qty,
                    position_value,
                    min_notional,
                )
                return False

        if not bool(diag.get("meets_min")):
            log.info(
                "skip %s SELL: filters reject qty=%s min_qty=%s min_notional=%s notional=%s",
                symbol,
                qty,
                min_qty,
                min_notional,
                qty * price,
            )
            return False
        agents = list(getattr(sig, "contributing_agents", []) or [])
        order = await self._submit(symbol, OrderSide.SELL, qty, agents)
        return self._order_filled(order)

    @staticmethod
    def _order_filled(order: Optional[Order]) -> bool:
        """True only if the exchange actually filled the order (partial counts).

        `_submit` can return a non-None Order that never filled — a live order
        rejected/expired by Binance, or a config-drift DRY_RUN status — without
        raising. Callers MUST check this before treating a BUY/SELL as executed;
        otherwise cooldowns, position slots, and risk high-water-marks get
        updated for a trade that never actually happened on the exchange.
        """
        if order is None:
            return False
        filled = order.filled_quantity or Decimal("0")
        return order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED) and filled > 0

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
            # Only mirror a live order into our book if the exchange actually
            # filled it. Recording the *requested* qty on a partial fill (or a
            # config-drift DRY_RUN) would leave a phantom/oversized position the
            # next SELL could try to over-dump. Use the executed quantity and
            # the real average fill price so our book matches Binance exactly.
            filled = order.filled_quantity or Decimal("0")
            if order.status not in (
                OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED
            ) or filled <= 0:
                log.error(
                    "LIVE order NOT filled — not recording: %s %s qty=%s status=%s "
                    "filled=%s coid=%s",
                    symbol, side.value, qty, order.status, filled,
                    order.client_order_id,
                )
                self.state.last_error = (
                    f"{symbol} {side.value} not filled (status={order.status})"
                )
                return order
            if filled < qty:
                log.warning(
                    "LIVE partial fill %s %s: requested=%s filled=%s coid=%s",
                    symbol, side.value, qty, filled, order.client_order_id,
                )
            try:
                price = order.avg_fill_price or order.price or await self._price(symbol)
                storage.record_order(
                    mode="live", symbol=symbol, side=side.value,
                    qty=filled, price=price,
                    client_order_id=order.client_order_id, agents=agents,
                )
                if side is OrderSide.BUY:
                    storage.open_position(
                        symbol=symbol, mode="live", qty=filled,
                        entry_price=price, agents=agents,
                    )
                else:
                    storage.close_position(symbol=symbol, mode="live", exit_price=price)
            except Exception as exc:  # noqa: BLE001
                log.warning("storage write failed for live order %s: %s", symbol, exc)
        self.state.trades_executed += 1
        return order


autopilot = Autopilot()
