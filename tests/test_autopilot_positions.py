"""Autopilot position-slot accounting tests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd

import app.data as data_module
import app.ta as ta_module
from app.exchange import Order, OrderSide, OrderStatus, OrderType
from app.trading import autopilot as autopilot_module
from app.trading.autopilot import Autopilot


class _FakeRepo:
    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    async def get(self, *_a, **_k) -> pd.DataFrame:
        return self._df


def _trend_settings(enabled: bool = True):
    class _S:
        trend_filter_enabled = enabled

    return _S()


def _patch_trend_data(monkeypatch, df: pd.DataFrame, *, enabled: bool = True) -> None:
    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _trend_settings(enabled))
    monkeypatch.setattr(data_module, "OHLCVRepository", lambda: _FakeRepo(df))
    monkeypatch.setattr(ta_module, "add_indicators", lambda d: d)


async def test_trend_gate_blocks_downtrend(monkeypatch):
    """A daily close below the 200-EMA must veto a new long."""
    ap = Autopilot()
    df = pd.DataFrame({"close": [11.0, 9.0], "ema_200": [12.0, 12.0]})
    _patch_trend_data(monkeypatch, df)
    ok, why = await ap._trend_gate("BTCUSDT")
    assert ok is False
    assert "downtrend" in why


async def test_trend_gate_allows_uptrend(monkeypatch):
    """A daily close at/above the 200-EMA must allow a new long."""
    ap = Autopilot()
    df = pd.DataFrame({"close": [11.0, 15.0], "ema_200": [12.0, 12.0]})
    _patch_trend_data(monkeypatch, df)
    ok, _why = await ap._trend_gate("BTCUSDT")
    assert ok is True


async def test_trend_gate_disabled_fail_open(monkeypatch):
    """When the filter is disabled the gate must always allow (fail-open)."""
    ap = Autopilot()
    df = pd.DataFrame({"close": [9.0], "ema_200": [12.0]})  # would block if enabled
    _patch_trend_data(monkeypatch, df, enabled=False)
    ok, why = await ap._trend_gate("BTCUSDT")
    assert ok is True
    assert why == "trend_disabled"


def _market_settings(enabled: bool = True):
    class _S:
        market_regime_gate_enabled = enabled

    return _S()


def _patch_market_data(monkeypatch, df: pd.DataFrame, *, enabled: bool = True) -> None:
    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _market_settings(enabled))
    monkeypatch.setattr(data_module, "OHLCVRepository", lambda: _FakeRepo(df))
    monkeypatch.setattr(ta_module, "add_indicators", lambda d: d)


async def test_market_gate_blocks_btc_downtrend(monkeypatch):
    """A confirmed multi-factor bear regime (falling ema50 well below ema200,
    price below ema50) must veto ALL new longs."""
    ap = Autopilot()
    df = pd.DataFrame(
        {
            "close": [110, 108, 106, 104, 102, 100, 98, 96],
            "ema_50": [130, 128, 126, 124, 122, 120, 118, 116],
            "ema_200": [150] * 8,
        }
    )
    _patch_market_data(monkeypatch, df)
    ok, why = await ap._market_gate()
    assert ok is False
    assert "risk-off" in why
    assert ap._last_regime_score <= -1


async def test_market_gate_allows_btc_uptrend(monkeypatch):
    """A confirmed multi-factor bull regime (rising ema50 well above ema200,
    price above ema50) must allow longs."""
    ap = Autopilot()
    df = pd.DataFrame(
        {
            "close": [90, 92, 94, 96, 98, 100, 102, 104],
            "ema_50": [70, 72, 74, 76, 78, 80, 82, 84],
            "ema_200": [75] * 8,
        }
    )
    _patch_market_data(monkeypatch, df)
    ok, why = await ap._market_gate()
    assert ok is True
    assert "risk-on" in why
    assert ap._last_regime_score >= 1


async def test_market_gate_disabled_fail_open(monkeypatch):
    """When disabled the market gate must always allow (fail-open)."""
    ap = Autopilot()
    df = pd.DataFrame({"close": [100.0], "ema_50": [90.0], "ema_200": [100.0]})  # would block
    _patch_market_data(monkeypatch, df, enabled=False)
    ok, why = await ap._market_gate()
    assert ok is True
    assert why == "market_gate_disabled"


async def test_market_gate_blocks_on_missing_data(monkeypatch):
    """Enabled + insufficient/empty BTC data must FAIL CLOSED (block new
    entries) — the bot must never open a position while it can't verify the
    broad market regime."""
    ap = Autopilot()
    df = pd.DataFrame({"close": [], "ema_50": [], "ema_200": []})
    _patch_market_data(monkeypatch, df, enabled=True)
    ok, why = await ap._market_gate()
    assert ok is False
    assert ap._last_regime_score == -1


async def test_market_gate_blocks_on_fetch_exception(monkeypatch):
    """Enabled + a data-fetch exception must FAIL CLOSED, not allow trading."""
    ap = Autopilot()

    class _RaisingRepo:
        async def get(self, *_a, **_k):
            raise RuntimeError("exchange unavailable")

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _market_settings(True))
    monkeypatch.setattr(data_module, "OHLCVRepository", lambda: _RaisingRepo())
    ok, why = await ap._market_gate()
    assert ok is False
    assert "market_unavailable" in why


async def test_tick_does_not_run_risk_gates_inline(monkeypatch):
    """Protective exits are owned by the independent risk loop, not tick()."""
    ap = Autopilot()
    ap.state.running = True

    class _S:
        paper_trading = True
        llm_in_trading_loop = False

    async def _boom(self):
        raise AssertionError("tick() should not call _run_risk_gates")

    async def _fake_breaker(self):
        return False

    async def _fake_execute(self, signals, allow_buys):
        assert signals == {}
        assert allow_buys is True

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _S())
    monkeypatch.setattr(autopilot_module.storage, "try_acquire_lock", lambda *a, **k: True, raising=True)
    monkeypatch.setattr(autopilot_module.storage, "release_lock", lambda *a, **k: None, raising=True)
    monkeypatch.setattr(autopilot_module.paper_exchange, "ensure_seeded", lambda: None, raising=True)
    monkeypatch.setattr(autopilot_module.filters, "load", lambda: asyncio.sleep(0), raising=True)
    monkeypatch.setattr(Autopilot, "_run_risk_gates", _boom, raising=True)
    monkeypatch.setattr(Autopilot, "_check_circuit_breaker", _fake_breaker, raising=True)
    monkeypatch.setattr(autopilot_module, "run_all_agents", lambda use_llm: asyncio.sleep(0, result={}), raising=True)
    monkeypatch.setattr(Autopilot, "_execute", _fake_execute, raising=True)


async def test_run_risk_gates_executes_stop_loss_even_when_entry_gates_blocked(monkeypatch):
    """Protective exits (app/trading/risk.py evaluate_exits, run via the
    independent risk loop) must fire regardless of ANY entry-side gate state
    — emergency halt, a failed BTC-regime check, a failed order-book check,
    etc. all block NEW BUYs only. This directly exercises `_run_risk_gates`
    end-to-end with `_entry_block_reason` forced to a blocking value and
    confirms a stop-loss SELL is still submitted."""
    ap = Autopilot()
    ap.state.mode = "paper"

    position = {
        "symbol": "ETHUSDT", "mode": "paper", "qty": 1.0,
        "entry_price": 100.0, "entry_ts": datetime.now(timezone.utc).isoformat(),
        "agents": "[]",
    }

    async def _fake_snapshot(**_kwargs):
        return {"free_balances": {"ETH": 1.0}, "all_balances": {"ETH": 1.0}}

    submitted: list[tuple] = []

    async def _fake_submit(self, symbol, side, qty, agents):
        submitted.append((symbol, side, qty, agents))
        return Order(
            symbol=symbol, side=side, type=OrderType.MARKET, quantity=qty,
            client_order_id="test", status=OrderStatus.FILLED,
            filled_quantity=qty, avg_fill_price=Decimal("90"),
        )

    monkeypatch.setattr(autopilot_module.storage, "all_positions", lambda: [position], raising=True)
    monkeypatch.setattr(autopilot_module, "portfolio_snapshot", _fake_snapshot, raising=True)
    monkeypatch.setattr(Autopilot, "_price", lambda self, symbol: asyncio.sleep(0, result=Decimal("90")), raising=True)
    monkeypatch.setattr(Autopilot, "_entry_block_reason", lambda self: "emergency_halt: simulated", raising=True)
    monkeypatch.setattr(autopilot_module.filters, "round_qty", lambda symbol, qty: qty, raising=True)
    monkeypatch.setattr(autopilot_module.filters, "meets_min", lambda symbol, qty, price: True, raising=True)
    monkeypatch.setattr(autopilot_module.storage, "record_tick_audit", lambda **_k: None, raising=True)
    monkeypatch.setattr(Autopilot, "_submit", _fake_submit, raising=True)

    # Confirm the (blocked) entry-protection state really is active — this
    # test would be meaningless if it weren't.
    assert ap._entry_block_reason() is not None

    await ap._run_risk_gates()

    assert len(submitted) == 1
    symbol, side, qty, agents = submitted[0]
    assert symbol == "ETHUSDT"
    assert side == OrderSide.SELL
    assert any(a == "risk:stop_loss" for a in agents)


async def test_count_non_dust_positions_excludes_dust(monkeypatch):
    """Dust balances must not consume one of max_open_positions slots."""
    ap = Autopilot()

    async def _fake_price(_self, _symbol: str) -> Decimal:
        return Decimal("100")

    monkeypatch.setattr(Autopilot, "_price", _fake_price, raising=True)

    def _round_qty(symbol: str, qty: Decimal) -> Decimal:
        # Simulate LOT_SIZE rounding: dust rounds down to zero.
        return Decimal("0") if symbol == "DUSTUSDT" else qty

    def _meets_min(symbol: str, qty: Decimal, price: Decimal) -> bool:
        # Simulate MIN_NOTIONAL: dust never passes.
        if symbol == "DUSTUSDT":
            return False
        return (qty * price) >= Decimal("10")

    monkeypatch.setattr(autopilot_module.filters, "round_qty", _round_qty, raising=True)
    monkeypatch.setattr(autopilot_module.filters, "meets_min", _meets_min, raising=True)

    open_positions = [
        {
            "symbol": "BTCUSDT",
            "qty": Decimal("0.20"),
            "entry_price": Decimal("100"),
            "mode": "paper",
        },
        {
            "symbol": "DUSTUSDT",
            "qty": Decimal("0.00000001"),
            "entry_price": Decimal("100"),
            "mode": "paper",
        },
    ]
    balances = {
        "BTC": Decimal("0.20"),
        "DUST": Decimal("0.00000001"),
    }

    count, held_symbols = await ap._count_non_dust_positions(
        open_positions=open_positions,
        balances=balances,
    )

    assert count == 1
    assert held_symbols == {"BTCUSDT"}


def _fake_order(status: OrderStatus, filled: str) -> Order:
    return Order(
        symbol="BTCUSDT",
        side=OrderSide.SELL,
        type=OrderType.MARKET,
        quantity=Decimal("1"),
        client_order_id="test-coid",
        status=status,
        filled_quantity=Decimal(filled),
    )


def test_order_filled_true_for_filled_order():
    order = _fake_order(OrderStatus.FILLED, "1")
    assert Autopilot._order_filled(order) is True


def test_order_filled_true_for_partial_fill():
    order = _fake_order(OrderStatus.PARTIALLY_FILLED, "0.5")
    assert Autopilot._order_filled(order) is True


def test_order_filled_false_for_rejected_order():
    """A live order Binance never filled must NOT be reported as executed.

    Regression: `_submit` can return a non-None, non-raising Order for a
    rejected/expired live order (or a config-drift DRY_RUN). Callers must
    check the fill status instead of assuming success.
    """
    order = _fake_order(OrderStatus.REJECTED, "0")
    assert Autopilot._order_filled(order) is False


def test_order_filled_false_for_dry_run_status():
    order = _fake_order(OrderStatus.DRY_RUN, "0")
    assert Autopilot._order_filled(order) is False


def test_order_filled_false_for_zero_fill_with_filled_status():
    """Defensive: even a FILLED status with zero quantity must not count."""
    order = _fake_order(OrderStatus.FILLED, "0")
    assert Autopilot._order_filled(order) is False


def test_order_filled_false_for_none():
    assert Autopilot._order_filled(None) is False


def test_dynamic_ml_gate_thresholds_follow_action(monkeypatch):
    ap = Autopilot()

    class _S:
        ml_gate_threshold = 0.50

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _S())

    assert ap._ml_gate_threshold_for_confidence(0.95, True, autopilot_module.SignalAction.BUY) == 0.40
    assert ap._ml_gate_threshold_for_confidence(0.85, True, autopilot_module.SignalAction.SELL) == 0.50
    assert ap._ml_gate_threshold_for_confidence(0.75, False, autopilot_module.SignalAction.BUY) == 0.40
    assert ap._ml_gate_threshold_for_confidence(0.65, False, autopilot_module.SignalAction.SELL) == 0.50


def test_signal_min_confidence_follows_action(monkeypatch):
    ap = Autopilot()

    assert ap._signal_min_confidence(autopilot_module.SignalAction.BUY) == 0.40
    assert ap._signal_min_confidence(autopilot_module.SignalAction.SELL) == 0.40


def test_trend_gate_bypass_requires_both_confidence_and_ml(monkeypatch):
    ap = Autopilot()

    class _S:
        trend_gate_bypass_confidence = 0.85
        trend_gate_bypass_ml_proba = 0.55

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _S())

    assert ap._trend_gate_bypass_allowed(0.90, 0.60, True) is True
    assert ap._trend_gate_bypass_allowed(0.84, 0.60, True) is False
    assert ap._trend_gate_bypass_allowed(0.90, 0.54, True) is False
    assert ap._trend_gate_bypass_allowed(0.90, None, True) is False


def test_aggressive_mode_rolls_back_below_min_win_rate(monkeypatch):
    ap = Autopilot()
    ap.state.mode = "live"

    class _S:
        aggressive_mode_enabled = True
        aggressive_rollback_min_trades = 30
        aggressive_rollback_min_win_rate = 0.50

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _S())
    monkeypatch.setattr(
        autopilot_module.storage,
        "closed_trades",
        lambda limit=30: [{"mode": "live", "pnl": 1}] * 14 + [{"mode": "live", "pnl": -1}] * 16,
        raising=True,
    )

    active, reason = ap._aggressive_mode_active()
    assert active is False
    assert "rollback" in reason


def test_aggressive_mode_stays_active_during_warmup(monkeypatch):
    ap = Autopilot()
    ap.state.mode = "live"

    class _S:
        aggressive_mode_enabled = True
        aggressive_rollback_min_trades = 30
        aggressive_rollback_min_win_rate = 0.50

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _S())
    monkeypatch.setattr(
        autopilot_module.storage,
        "closed_trades",
        lambda limit=30: [{"mode": "live", "pnl": 1}] * 10,
        raising=True,
    )

    active, reason = ap._aggressive_mode_active()
    assert active is True
    assert reason.startswith("warmup:")


class _Sig:
    contributing_agents = ["test"]


async def test_place_buy_returns_false_when_order_not_filled(monkeypatch):
    """`_place_buy` must propagate a non-filled `_submit` result as failure."""
    ap = Autopilot()

    async def _fake_price(_self, _symbol: str) -> Decimal:
        return Decimal("100")

    async def _fake_submit(_self, *_a, **_k) -> Order:
        return _fake_order(OrderStatus.REJECTED, "0")

    monkeypatch.setattr(Autopilot, "_price", _fake_price, raising=True)
    monkeypatch.setattr(Autopilot, "_submit", _fake_submit, raising=True)
    monkeypatch.setattr(autopilot_module.filters, "round_qty", lambda s, q: q, raising=True)
    monkeypatch.setattr(autopilot_module.filters, "meets_min", lambda s, q, p: True, raising=True)

    placed = await ap._place_buy("BTCUSDT", _Sig(), Decimal("100"))
    assert placed is False


async def test_place_sell_returns_false_when_order_not_filled(monkeypatch):
    """`_place_sell` must propagate a non-filled `_submit` result as failure."""
    ap = Autopilot()

    async def _fake_price(_self, _symbol: str) -> Decimal:
        return Decimal("100")

    async def _fake_submit(_self, *_a, **_k) -> Order:
        return _fake_order(OrderStatus.REJECTED, "0")

    monkeypatch.setattr(Autopilot, "_price", _fake_price, raising=True)
    monkeypatch.setattr(Autopilot, "_submit", _fake_submit, raising=True)
    monkeypatch.setattr(autopilot_module.filters, "round_qty", lambda s, q: q, raising=True)
    monkeypatch.setattr(autopilot_module.filters, "meets_min", lambda s, q, p: True, raising=True)

    placed = await ap._place_sell("BTCUSDT", _Sig(), Decimal("1"))
    assert placed is False


async def test_place_sell_returns_true_when_order_filled(monkeypatch):
    ap = Autopilot()

    async def _fake_price(_self, _symbol: str) -> Decimal:
        return Decimal("100")

    async def _fake_submit(_self, *_a, **_k) -> Order:
        return _fake_order(OrderStatus.FILLED, "1")

    monkeypatch.setattr(Autopilot, "_price", _fake_price, raising=True)
    monkeypatch.setattr(Autopilot, "_submit", _fake_submit, raising=True)
    monkeypatch.setattr(autopilot_module.filters, "round_qty", lambda s, q: q, raising=True)
    monkeypatch.setattr(autopilot_module.filters, "meets_min", lambda s, q, p: True, raising=True)

    placed = await ap._place_sell("BTCUSDT", _Sig(), Decimal("1"))
    assert placed is True


async def test_execute_sell_uses_live_balance_without_local_position(monkeypatch):
    """Live SELLs must liquidate real holdings even if the local book drifted."""
    ap = Autopilot()
    ap.state.mode = "live"

    class _Settings:
        min_signal_confidence = 0.55
        dynamic_threshold_enabled = False
        ml_gate_enabled = False
        buy_cooldown_minutes = 30

    class _Snap(dict):
        pass

    async def _fake_snapshot(*, mode):
        assert mode == "live"
        return _Snap({
            "usdt_cash": Decimal("100"),
            "total_usdt": Decimal("150"),
            "all_balances": {"BTC": Decimal("0.25"), "USDT": Decimal("100")},
        })

    placed: list[tuple[str, Decimal]] = []

    async def _fake_place_sell(_self, symbol: str, _sig, free: Decimal) -> bool:
        placed.append((symbol, free))
        return True

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _Settings())
    monkeypatch.setattr(autopilot_module, "portfolio_snapshot", _fake_snapshot)
    monkeypatch.setattr(autopilot_module.storage, "all_positions", lambda: [])
    monkeypatch.setattr(autopilot_module, "online_regime", autopilot_module.online_regime)
    monkeypatch.setattr(Autopilot, "_count_non_dust_positions", lambda *_a, **_k: __import__("asyncio").sleep(0, result=(0, set())), raising=True)
    monkeypatch.setattr(Autopilot, "_place_sell", _fake_place_sell, raising=True)
    monkeypatch.setattr(autopilot_module.filters, "is_listed", lambda _s: True, raising=True)
    monkeypatch.setattr(Autopilot, "_record_signal_event", lambda *_a, **_k: __import__("asyncio").sleep(0), raising=True)
    monkeypatch.setattr(Autopilot, "_persist_skip_stats", lambda *_a, **_k: None, raising=True)

    class _SellSig:
        action = autopilot_module.SignalAction.SELL
        confidence = 0.9
        contributing_agents = ["test"]
        timeframe = autopilot_module.Timeframe.D1

    await ap._execute({"BTCUSDT": _SellSig()}, allow_buys=True)

    assert placed == [("BTCUSDT", Decimal("0.25"))]


async def test_execute_sell_prefers_free_balance_over_total_balance(monkeypatch):
    """SELL sizing must use free balance, not free+locked total balance."""
    ap = Autopilot()
    ap.state.mode = "live"

    class _Settings:
        min_signal_confidence = 0.55
        dynamic_threshold_enabled = False
        ml_gate_enabled = False
        buy_cooldown_minutes = 30

    async def _fake_snapshot(*, mode):
        assert mode == "live"
        return {
            "usdt_cash": Decimal("100"),
            "total_usdt": Decimal("150"),
            "all_balances": {"BTC": Decimal("0.25"), "USDT": Decimal("100")},
            "free_balances": {"BTC": Decimal("0.20"), "USDT": Decimal("100")},
        }

    placed: list[tuple[str, Decimal]] = []

    async def _fake_place_sell(_self, symbol: str, _sig, free: Decimal) -> bool:
        placed.append((symbol, free))
        return True

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _Settings())
    monkeypatch.setattr(autopilot_module, "portfolio_snapshot", _fake_snapshot)
    monkeypatch.setattr(autopilot_module.storage, "all_positions", lambda: [])
    monkeypatch.setattr(
        Autopilot,
        "_count_non_dust_positions",
        lambda *_a, **_k: __import__("asyncio").sleep(0, result=(0, set())),
        raising=True,
    )
    monkeypatch.setattr(Autopilot, "_place_sell", _fake_place_sell, raising=True)
    monkeypatch.setattr(autopilot_module.filters, "is_listed", lambda _s: True, raising=True)
    monkeypatch.setattr(Autopilot, "_record_signal_event", lambda *_a, **_k: __import__("asyncio").sleep(0), raising=True)
    monkeypatch.setattr(Autopilot, "_persist_skip_stats", lambda *_a, **_k: None, raising=True)

    class _SellSig:
        action = autopilot_module.SignalAction.SELL
        confidence = 0.9
        contributing_agents = ["test"]
        timeframe = autopilot_module.Timeframe.D1

    await ap._execute({"BTCUSDT": _SellSig()}, allow_buys=True)

    assert placed == [("BTCUSDT", Decimal("0.20"))]


async def test_execute_forces_take_profit_exit_before_buy_path(monkeypatch):
    ap = Autopilot()
    ap.state.mode = "live"

    class _Settings:
        min_signal_confidence = 0.40
        dynamic_threshold_enabled = False
        ml_gate_enabled = False
        buy_cooldown_minutes = 30
        aggressive_mode_enabled = True
        aggressive_rollback_min_trades = 30
        aggressive_rollback_min_win_rate = 0.50

    async def _fake_snapshot(*, mode):
        assert mode == "live"
        return {
            "usdt_cash": Decimal("100"),
            "total_usdt": Decimal("200"),
            "all_balances": {"BTC": Decimal("1"), "USDT": Decimal("100")},
        }

    placed: list[tuple[str, Decimal]] = []
    buys: list[tuple[str, Decimal]] = []

    async def _fake_place_sell(_self, symbol: str, _sig, free: Decimal) -> bool:
        placed.append((symbol, free))
        return True

    async def _fake_place_buy(_self, symbol: str, _sig, per_trade_usdt: Decimal) -> bool:
        buys.append((symbol, per_trade_usdt))
        return True

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _Settings())
    monkeypatch.setattr(autopilot_module, "portfolio_snapshot", _fake_snapshot)
    monkeypatch.setattr(autopilot_module.storage, "all_positions", lambda: [{"symbol": "BTCUSDT", "mode": "live", "qty": 1, "entry_price": 100, "entry_ts": "2026-07-21T00:00:00+00:00", "agents": "[]"}])
    monkeypatch.setattr(autopilot_module, "liquidity_gate", lambda *_a, **_k: __import__("asyncio").sleep(0, result=(True, "ok")))
    monkeypatch.setattr(autopilot_module.filters, "is_listed", lambda _s: True, raising=True)
    monkeypatch.setattr(Autopilot, "_price", lambda *_a, **_k: asyncio.sleep(0, result=Decimal("104")), raising=True)
    monkeypatch.setattr(Autopilot, "_record_signal_event", lambda *_a, **_k: __import__("asyncio").sleep(0), raising=True)
    monkeypatch.setattr(Autopilot, "_persist_skip_stats", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(Autopilot, "_persist_gate_stats", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(Autopilot, "_place_sell", _fake_place_sell, raising=True)
    monkeypatch.setattr(Autopilot, "_place_buy", _fake_place_buy, raising=True)

    class _BuySig:
        action = autopilot_module.SignalAction.BUY
        confidence = 0.82
        contributing_agents = ["test"]
        timeframe = autopilot_module.Timeframe.D1

    await ap._execute({"BTCUSDT": _BuySig()}, allow_buys=True)

    assert placed == [("BTCUSDT", Decimal("1"))]
    assert buys == []


async def test_execute_forces_exit_on_high_confidence_sell_signal(monkeypatch):
    ap = Autopilot()
    ap.state.mode = "live"

    class _Settings:
        min_signal_confidence = 0.40
        dynamic_threshold_enabled = False
        ml_gate_enabled = False
        buy_cooldown_minutes = 30
        aggressive_mode_enabled = True
        aggressive_rollback_min_trades = 30
        aggressive_rollback_min_win_rate = 0.50

    async def _fake_snapshot(*, mode):
        assert mode == "live"
        return {
            "usdt_cash": Decimal("100"),
            "total_usdt": Decimal("200"),
            "all_balances": {"BTC": Decimal("1"), "USDT": Decimal("100")},
        }

    placed: list[tuple[str, Decimal]] = []

    async def _fake_place_sell(_self, symbol: str, _sig, free: Decimal) -> bool:
        placed.append((symbol, free))
        return True

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _Settings())
    monkeypatch.setattr(autopilot_module, "portfolio_snapshot", _fake_snapshot)
    monkeypatch.setattr(autopilot_module.storage, "all_positions", lambda: [{"symbol": "BTCUSDT", "mode": "live", "qty": 1, "entry_price": 100, "entry_ts": "2026-07-21T00:00:00+00:00", "agents": "[]"}])
    monkeypatch.setattr(autopilot_module, "liquidity_gate", lambda *_a, **_k: __import__("asyncio").sleep(0, result=(True, "ok")))
    monkeypatch.setattr(autopilot_module.filters, "is_listed", lambda _s: True, raising=True)
    monkeypatch.setattr(Autopilot, "_price", lambda *_a, **_k: asyncio.sleep(0, result=Decimal("101")), raising=True)
    monkeypatch.setattr(Autopilot, "_record_signal_event", lambda *_a, **_k: __import__("asyncio").sleep(0), raising=True)
    monkeypatch.setattr(Autopilot, "_persist_skip_stats", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(Autopilot, "_persist_gate_stats", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(Autopilot, "_place_sell", _fake_place_sell, raising=True)

    class _SellSig:
        action = autopilot_module.SignalAction.SELL
        confidence = 0.71
        contributing_agents = ["test"]
        timeframe = autopilot_module.Timeframe.D1

    await ap._execute({"BTCUSDT": _SellSig()}, allow_buys=True)

    assert placed == [("BTCUSDT", Decimal("1"))]


async def test_execute_buy_pyramids_when_position_exists_and_confidence_is_high(monkeypatch):
    ap = Autopilot()
    ap.state.mode = "live"

    kv_state = {"pyramid_adds:live:BTCUSDT": 1}
    placed: list[tuple[str, Decimal]] = []

    class _Settings:
        min_signal_confidence = 0.40
        dynamic_threshold_enabled = False
        ml_gate_enabled = False
        buy_cooldown_minutes = 30
        aggressive_mode_enabled = True
        aggressive_rollback_min_trades = 30
        aggressive_rollback_min_win_rate = 0.50
        aggressive_position_pct = 0.06
        aggressive_max_open_positions = 10
        max_long_exposure_pct = 0.99
        risk_per_trade_pct = 0.01
        stop_loss_pct = 0.015
        kelly_fraction_cap = 1.0

    async def _ok(*_a, **_k):
        return True, "ok"

    async def _snapshot(*, mode):
        assert mode == "live"
        return {
            "usdt_cash": Decimal("100"),
            "total_usdt": Decimal("1000"),
            "all_balances": {"BTC": Decimal("1"), "USDT": Decimal("100")},
            "free_balances": {"BTC": Decimal("1"), "USDT": Decimal("100")},
        }

    async def _buy_plan(_self, _symbol: str, per_trade_usdt: Decimal):
        return {
            "price": Decimal("100"),
            "per_trade_usdt": per_trade_usdt,
            "raw_qty": Decimal("0.5"),
            "rounded_qty": Decimal("0.5000"),
            "notional": Decimal("50.0000"),
            "min_qty": Decimal("0.0001"),
            "min_notional": Decimal("10"),
            "meets_min": True,
            "qty_ok": True,
            "notional_ok": True,
        }

    async def _place_buy(_self, symbol: str, _sig, per_trade_usdt: Decimal) -> bool:
        placed.append((symbol, per_trade_usdt))
        return True

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _Settings())
    monkeypatch.setattr(autopilot_module, "portfolio_snapshot", _snapshot)
    monkeypatch.setattr(autopilot_module.storage, "all_positions", lambda: [{"symbol": "BTCUSDT", "mode": "live", "qty": 1, "entry_price": 100, "entry_ts": "2026-07-21T00:00:00+00:00", "agents": "[]"}])
    monkeypatch.setattr(autopilot_module.storage, "closed_trades", lambda limit=30: [{"mode": "live", "pnl": 1}] * 10)
    monkeypatch.setattr(autopilot_module.storage, "kv_get", lambda key, default=None: kv_state.get(key, default))
    monkeypatch.setattr(autopilot_module.storage, "kv_set", lambda key, value: kv_state.__setitem__(key, value))
    monkeypatch.setattr(Autopilot, "_price", lambda *_a, **_k: asyncio.sleep(0, result=Decimal("100")), raising=True)
    monkeypatch.setattr(Autopilot, "_count_non_dust_positions", lambda *_a, **_k: asyncio.sleep(0, result=(1, {"BTCUSDT"})), raising=True)
    monkeypatch.setattr(Autopilot, "_record_signal_event", lambda *_a, **_k: asyncio.sleep(0), raising=True)
    monkeypatch.setattr(Autopilot, "_persist_skip_stats", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(Autopilot, "_persist_gate_stats", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(autopilot_module.trade_audit_logger, "log_event", lambda **_k: None)
    monkeypatch.setattr(autopilot_module.filters, "is_listed", lambda _s: True, raising=True)
    monkeypatch.setattr(autopilot_module, "liquidity_gate", _ok)
    monkeypatch.setattr(Autopilot, "_market_gate", lambda *_a, **_k: asyncio.sleep(0, result=(True, "ok")), raising=True)
    monkeypatch.setattr(Autopilot, "_trend_gate", lambda *_a, **_k: asyncio.sleep(0, result=(False, "downtrend")), raising=True)
    monkeypatch.setattr(Autopilot, "_funding_gate", lambda *_a, **_k: asyncio.sleep(0, result=(True, "ok")), raising=True)
    monkeypatch.setattr(Autopilot, "_onchain_gate", lambda *_a, **_k: asyncio.sleep(0, result=(True, "ok")), raising=True)
    monkeypatch.setattr(Autopilot, "_buy_order_plan", _buy_plan, raising=True)
    monkeypatch.setattr(Autopilot, "_place_buy", _place_buy, raising=True)
    monkeypatch.setattr(autopilot_module.RiskManager, "evaluate_entry", lambda *_a, **_k: __import__("types").SimpleNamespace(allow=True, reason="ok", notional_usdt=Decimal("50")), raising=True)

    class _Sig:
        action = autopilot_module.SignalAction.BUY
        confidence = 0.80
        agent = "profitstream_strategy"
        contributing_agents = ["profitstream_strategy"]
        quality_score = 90
        timeframe = autopilot_module.Timeframe.D1

    await ap._execute({"BTCUSDT": _Sig()}, allow_buys=True)

    assert placed == [("BTCUSDT", Decimal("50"))]
    assert kv_state["pyramid_adds:live:BTCUSDT"] == 2


def test_profitstream_trend_adjustment_is_soft_and_legacy_stays_blocked():
    ap = Autopilot()

    class _ProfitStreamSignal:
        agent = "profitstream_strategy"
        contributing_agents = ("profitstream_strategy",)
        quality_score = 90

    allowed, why, adjusted = ap._profitstream_trend_adjustment(
        _ProfitStreamSignal(), False, "downtrend"
    )
    assert allowed is True
    assert adjusted == 80
    assert "soft_penalty" in why

    class _LegacySignal:
        agent = "trend_follower"
        contributing_agents = ("trend_follower",)
        quality_score = 90

    allowed, why, adjusted = ap._profitstream_trend_adjustment(
        _LegacySignal(), False, "downtrend"
    )
    assert allowed is False
    assert why == "downtrend"
    assert adjusted is None


async def test_execute_buy_rejects_when_pyramid_limit_is_reached(monkeypatch):
    ap = Autopilot()
    ap.state.mode = "live"

    kv_state = {"pyramid_adds:live:BTCUSDT": 2}
    placed: list[tuple[str, Decimal]] = []

    class _Settings:
        min_signal_confidence = 0.40
        dynamic_threshold_enabled = False
        ml_gate_enabled = False
        buy_cooldown_minutes = 30
        aggressive_mode_enabled = True
        aggressive_rollback_min_trades = 30
        aggressive_rollback_min_win_rate = 0.50
        aggressive_position_pct = 0.06
        aggressive_max_open_positions = 10
        max_long_exposure_pct = 0.99
        risk_per_trade_pct = 0.01
        stop_loss_pct = 0.015
        kelly_fraction_cap = 1.0

    async def _ok(*_a, **_k):
        return True, "ok"

    async def _snapshot(*, mode):
        assert mode == "live"
        return {
            "usdt_cash": Decimal("100"),
            "total_usdt": Decimal("1000"),
            "all_balances": {"BTC": Decimal("1"), "USDT": Decimal("100")},
            "free_balances": {"BTC": Decimal("1"), "USDT": Decimal("100")},
        }

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _Settings())
    monkeypatch.setattr(autopilot_module, "portfolio_snapshot", _snapshot)
    monkeypatch.setattr(autopilot_module.storage, "all_positions", lambda: [{"symbol": "BTCUSDT", "mode": "live", "qty": 1, "entry_price": 100, "entry_ts": "2026-07-21T00:00:00+00:00", "agents": "[]"}])
    monkeypatch.setattr(autopilot_module.storage, "closed_trades", lambda limit=30: [{"mode": "live", "pnl": 1}] * 10)
    monkeypatch.setattr(autopilot_module.storage, "kv_get", lambda key, default=None: kv_state.get(key, default))
    monkeypatch.setattr(autopilot_module.storage, "kv_set", lambda key, value: kv_state.__setitem__(key, value))
    monkeypatch.setattr(Autopilot, "_price", lambda *_a, **_k: asyncio.sleep(0, result=Decimal("100")), raising=True)
    monkeypatch.setattr(Autopilot, "_count_non_dust_positions", lambda *_a, **_k: asyncio.sleep(0, result=(1, {"BTCUSDT"})), raising=True)
    monkeypatch.setattr(Autopilot, "_record_signal_event", lambda *_a, **_k: asyncio.sleep(0), raising=True)
    monkeypatch.setattr(Autopilot, "_persist_skip_stats", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(Autopilot, "_persist_gate_stats", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(autopilot_module.trade_audit_logger, "log_event", lambda **_k: None)
    monkeypatch.setattr(autopilot_module.filters, "is_listed", lambda _s: True, raising=True)
    monkeypatch.setattr(autopilot_module, "liquidity_gate", _ok)
    monkeypatch.setattr(Autopilot, "_market_gate", lambda *_a, **_k: asyncio.sleep(0, result=(True, "ok")), raising=True)
    monkeypatch.setattr(Autopilot, "_trend_gate", lambda *_a, **_k: asyncio.sleep(0, result=(True, "ok")), raising=True)
    monkeypatch.setattr(Autopilot, "_funding_gate", lambda *_a, **_k: asyncio.sleep(0, result=(True, "ok")), raising=True)
    monkeypatch.setattr(Autopilot, "_onchain_gate", lambda *_a, **_k: asyncio.sleep(0, result=(True, "ok")), raising=True)
    monkeypatch.setattr(Autopilot, "_buy_order_plan", lambda *_a, **_k: asyncio.sleep(0, result={"price": Decimal("100"), "per_trade_usdt": Decimal("50"), "raw_qty": Decimal("0.5"), "rounded_qty": Decimal("0.5000"), "notional": Decimal("50.0000"), "min_qty": Decimal("0.0001"), "min_notional": Decimal("10"), "meets_min": True, "qty_ok": True, "notional_ok": True}), raising=True)
    monkeypatch.setattr(Autopilot, "_place_buy", lambda *_a, **_k: asyncio.sleep(0, result=True), raising=True)
    monkeypatch.setattr(autopilot_module.RiskManager, "evaluate_entry", lambda *_a, **_k: __import__("types").SimpleNamespace(allow=True, reason="ok", notional_usdt=Decimal("50")), raising=True)

    class _Sig:
        action = autopilot_module.SignalAction.BUY
        confidence = 0.80
        contributing_agents = ["test"]
        timeframe = autopilot_module.Timeframe.D1

    await ap._execute({"BTCUSDT": _Sig()}, allow_buys=True)

    assert placed == []
    assert kv_state["pyramid_adds:live:BTCUSDT"] == 2


async def test_execute_buy_rejects_blocked_symbol(monkeypatch):
    """Defense-in-depth: a symbol in blocked_symbols must never receive a new
    entry even if it somehow reaches `_execute` (the universe builder already
    excludes it, but this is a second, independent hard stop)."""
    ap = Autopilot()
    ap.state.mode = "live"
    placed: list[tuple[str, Decimal]] = []

    class _Settings:
        min_signal_confidence = 0.40
        dynamic_threshold_enabled = False
        ml_gate_enabled = False
        buy_cooldown_minutes = 30
        aggressive_mode_enabled = False
        aggressive_rollback_min_trades = 30
        aggressive_rollback_min_win_rate = 0.50
        rollback_position_pct = 0.05
        rollback_max_open_positions = 10
        max_long_exposure_pct = 0.99
        risk_per_trade_pct = 0.01
        stop_loss_pct = 0.015
        kelly_fraction_cap = 1.0
        blocked_symbols = ("BTCUSDT",)

    async def _snapshot(*, mode):
        return {
            "usdt_cash": Decimal("100"),
            "total_usdt": Decimal("1000"),
            "all_balances": {"USDT": Decimal("100")},
            "free_balances": {"USDT": Decimal("100")},
        }

    async def _place_buy(_self, symbol: str, _sig, per_trade_usdt: Decimal) -> bool:
        placed.append((symbol, per_trade_usdt))
        return True

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _Settings())
    monkeypatch.setattr(autopilot_module, "portfolio_snapshot", _snapshot)
    monkeypatch.setattr(autopilot_module.storage, "all_positions", lambda: [])
    monkeypatch.setattr(autopilot_module.storage, "kv_get", lambda key, default=None: default)
    monkeypatch.setattr(autopilot_module.storage, "kv_set", lambda key, value: None)
    monkeypatch.setattr(Autopilot, "_price", lambda *_a, **_k: asyncio.sleep(0, result=Decimal("100")), raising=True)
    monkeypatch.setattr(Autopilot, "_count_non_dust_positions", lambda *_a, **_k: asyncio.sleep(0, result=(0, set())), raising=True)
    monkeypatch.setattr(Autopilot, "_record_signal_event", lambda *_a, **_k: asyncio.sleep(0), raising=True)
    monkeypatch.setattr(Autopilot, "_persist_skip_stats", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(Autopilot, "_persist_gate_stats", lambda *_a, **_k: None, raising=True)
    monkeypatch.setattr(autopilot_module.trade_audit_logger, "log_event", lambda **_k: None)
    monkeypatch.setattr(autopilot_module.filters, "is_listed", lambda _s: True, raising=True)
    monkeypatch.setattr(Autopilot, "_place_buy", _place_buy, raising=True)

    class _Sig:
        action = autopilot_module.SignalAction.BUY
        confidence = 0.80
        contributing_agents = ["test"]
        timeframe = autopilot_module.Timeframe.D1

    await ap._execute({"BTCUSDT": _Sig()}, allow_buys=True)

    assert placed == []


async def test_buy_trace_persists_market_gate_and_sizing(monkeypatch):
    ap = Autopilot()
    ap.state.mode = "live"

    class _Settings:
        min_signal_confidence = 0.55
        dynamic_threshold_enabled = False
        ml_gate_enabled = False
        buy_cooldown_minutes = 30
        aggressive_mode_enabled = False
        max_position_pct = 0.05

    captured = {}

    async def _fake_snapshot(*, mode):
        assert mode == "live"
        return {
            "usdt_cash": Decimal("100"),
            "total_usdt": Decimal("100"),
            "all_balances": {"USDT": Decimal("100")},
        }

    async def _fake_count(*_a, **_k):
        return 0, set()

    async def _fake_atr(_self, _symbol: str):
        return 0.02

    async def _fake_market_gate(_self):
        return False, "BTC risk-off"

    async def _fake_price(_self, _symbol: str):
        return Decimal("50")

    def _capture(_self, counter, tick_debug, *, total):
        captured["counter"] = dict(counter)
        captured["tick_debug"] = tick_debug
        captured["total"] = total

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _Settings())
    monkeypatch.setattr(autopilot_module, "portfolio_snapshot", _fake_snapshot)
    monkeypatch.setattr(autopilot_module.storage, "all_positions", lambda: [])
    monkeypatch.setattr(autopilot_module.storage, "closed_trades", lambda limit=100: [])
    monkeypatch.setattr(Autopilot, "_count_non_dust_positions", _fake_count, raising=True)
    monkeypatch.setattr(Autopilot, "_atr_pct", _fake_atr, raising=True)
    monkeypatch.setattr(Autopilot, "_market_gate", _fake_market_gate, raising=True)
    monkeypatch.setattr(Autopilot, "_price", _fake_price, raising=True)
    monkeypatch.setattr(Autopilot, "_trend_gate", lambda *_a, **_k: __import__("asyncio").sleep(0, result=(True, "ok")), raising=True)
    monkeypatch.setattr(Autopilot, "_funding_gate", lambda *_a, **_k: __import__("asyncio").sleep(0, result=(True, "ok")), raising=True)
    monkeypatch.setattr(Autopilot, "_onchain_gate", lambda *_a, **_k: __import__("asyncio").sleep(0, result=(True, "ok")), raising=True)
    monkeypatch.setattr(Autopilot, "_record_signal_event", lambda *_a, **_k: __import__("asyncio").sleep(0), raising=True)
    monkeypatch.setattr(Autopilot, "_persist_skip_stats", _capture, raising=True)
    monkeypatch.setattr(autopilot_module.filters, "is_listed", lambda _s: True, raising=True)
    monkeypatch.setattr(autopilot_module.filters, "round_qty", lambda _s, q: q.quantize(Decimal("0.0001")), raising=True)
    monkeypatch.setattr(
        autopilot_module.filters,
        "diagnostics",
        lambda _s, qty, price: {
            "min_qty": Decimal("0.0001"),
            "min_notional": Decimal("10"),
            "qty_ok": True,
            "notional_ok": True,
            "meets_min": True,
            "qty": qty,
            "price": price,
            "notional": qty * price,
        },
        raising=True,
    )

    class _BuySig:
        action = autopilot_module.SignalAction.BUY
        confidence = 0.9
        contributing_agents = ["test"]
        timeframe = autopilot_module.Timeframe.D1

    await ap._execute({"BTCUSDT": _BuySig()}, allow_buys=True)

    info = captured["tick_debug"]["BTCUSDT"]
    assert captured["counter"]["market_gate"] == 1
    assert info["action"] == "BUY"
    assert info["filters"]["market_regime"]["ok"] is False
    assert info["filters"]["min_notional"]["ok"] is True
    assert info["sizing"]["rounded_qty"] == "0.0600"
    assert info["sizing"]["notional"] == "3.0000"
    assert info["final_reason"] == "market_gate"
    assert info["submitted"] is False


# ── stop-loss re-entry cooldown ─────────────────────────────────────────────


def test_stop_loss_cooldown_blocks_immediate_reentry(monkeypatch):
    ap = Autopilot()
    ap.state.mode = "live"

    class _S:
        stop_loss_cooldown_minutes = 60

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _S())
    recent_stop = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    monkeypatch.setattr(
        autopilot_module.storage,
        "closed_trades",
        lambda limit=10: [
            {"symbol": "BTCUSDT", "mode": "live", "exit_reason": "stop_loss", "exit_ts": recent_stop}
        ],
        raising=True,
    )

    cooled, why = ap._stop_loss_cooldown_active("BTCUSDT")
    assert cooled is True
    assert "stop_loss_cooldown" in why


def test_stop_loss_cooldown_clears_after_window(monkeypatch):
    ap = Autopilot()
    ap.state.mode = "live"

    class _S:
        stop_loss_cooldown_minutes = 60

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _S())
    old_stop = (datetime.now(timezone.utc) - timedelta(minutes=90)).isoformat()
    monkeypatch.setattr(
        autopilot_module.storage,
        "closed_trades",
        lambda limit=10: [
            {"symbol": "BTCUSDT", "mode": "live", "exit_reason": "stop_loss", "exit_ts": old_stop}
        ],
        raising=True,
    )

    cooled, why = ap._stop_loss_cooldown_active("BTCUSDT")
    assert cooled is False
    assert why == "ok"


def test_stop_loss_cooldown_ignores_non_stop_loss_exit(monkeypatch):
    ap = Autopilot()
    ap.state.mode = "live"

    class _S:
        stop_loss_cooldown_minutes = 60

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _S())
    recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    monkeypatch.setattr(
        autopilot_module.storage,
        "closed_trades",
        lambda limit=10: [
            {"symbol": "BTCUSDT", "mode": "live", "exit_reason": "take_profit_1", "exit_ts": recent}
        ],
        raising=True,
    )

    cooled, why = ap._stop_loss_cooldown_active("BTCUSDT")
    assert cooled is False
    assert why == "ok"


# ── correlation / basket exposure cap ───────────────────────────────────────


def _corr_settings(**overrides):
    class _S:
        correlation_gate_enabled = True
        correlated_symbol_groups = (("BTCUSDT", "ETHUSDT", "SOLUSDT"),)
        max_correlated_exposure_pct = 0.35

    for k, v in overrides.items():
        setattr(_S, k, v)
    return _S()


async def test_correlation_basket_gate_blocks_over_cap(monkeypatch):
    ap = Autopilot()
    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _corr_settings())

    async def _fake_price(_self, symbol: str) -> Decimal:
        return Decimal("100")

    monkeypatch.setattr(Autopilot, "_price", _fake_price, raising=True)
    open_positions = [{"symbol": "ETHUSDT", "qty": Decimal("3"), "mode": "live"}]  # $300 held

    ok, why = await ap._correlation_basket_gate(
        "BTCUSDT", Decimal("100"), open_positions, Decimal("1000")
    )
    # (300 held + 100 proposed) / 1000 = 40% > 35% cap
    assert ok is False
    assert "correlated_basket_exposure" in why


async def test_correlation_basket_gate_allows_under_cap(monkeypatch):
    ap = Autopilot()
    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _corr_settings())

    async def _fake_price(_self, symbol: str) -> Decimal:
        return Decimal("100")

    monkeypatch.setattr(Autopilot, "_price", _fake_price, raising=True)
    open_positions = [{"symbol": "ETHUSDT", "qty": Decimal("1"), "mode": "live"}]  # $100 held

    ok, why = await ap._correlation_basket_gate(
        "BTCUSDT", Decimal("100"), open_positions, Decimal("1000")
    )
    # (100 held + 100 proposed) / 1000 = 20% < 35% cap
    assert ok is True


async def test_correlation_basket_gate_disabled_allows(monkeypatch):
    ap = Autopilot()
    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _corr_settings(correlation_gate_enabled=False))

    ok, why = await ap._correlation_basket_gate(
        "BTCUSDT", Decimal("100"), [{"symbol": "ETHUSDT", "qty": Decimal("100"), "mode": "live"}], Decimal("1000")
    )
    assert ok is True
    assert why == "correlation_gate_disabled"


async def test_correlation_basket_gate_symbol_not_in_group_allows(monkeypatch):
    ap = Autopilot()
    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _corr_settings())

    ok, why = await ap._correlation_basket_gate(
        "DOGEUSDT", Decimal("100"), [], Decimal("1000")
    )
    assert ok is True
    assert why == "not_in_correlated_group"

