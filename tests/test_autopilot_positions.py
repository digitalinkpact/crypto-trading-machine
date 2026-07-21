"""Autopilot position-slot accounting tests."""
from __future__ import annotations

import asyncio
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
    """BTC 50-EMA below 200-EMA (death cross) must veto ALL new longs."""
    ap = Autopilot()
    df = pd.DataFrame({"close": [100.0], "ema_50": [90.0], "ema_200": [100.0]})
    _patch_market_data(monkeypatch, df)
    ok, why = await ap._market_gate()
    assert ok is False
    assert "risk-off" in why


async def test_market_gate_allows_btc_uptrend(monkeypatch):
    """BTC 50-EMA at/above 200-EMA (golden cross) must allow longs."""
    ap = Autopilot()
    df = pd.DataFrame({"close": [100.0], "ema_50": [110.0], "ema_200": [100.0]})
    _patch_market_data(monkeypatch, df)
    ok, why = await ap._market_gate()
    assert ok is True
    assert "risk-on" in why


async def test_market_gate_disabled_fail_open(monkeypatch):
    """When disabled the market gate must always allow (fail-open)."""
    ap = Autopilot()
    df = pd.DataFrame({"close": [100.0], "ema_50": [90.0], "ema_200": [100.0]})  # would block
    _patch_market_data(monkeypatch, df, enabled=False)
    ok, why = await ap._market_gate()
    assert ok is True
    assert why == "market_gate_disabled"


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
        ml_gate_threshold_conf_70 = 0.45
        ml_gate_threshold_conf_80 = 0.40
        ml_gate_threshold_conf_90 = 0.35

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _S())

    assert ap._ml_gate_threshold_for_confidence(0.95, True, autopilot_module.SignalAction.BUY) == 0.35
    assert ap._ml_gate_threshold_for_confidence(0.85, True, autopilot_module.SignalAction.SELL) == 0.50
    assert ap._ml_gate_threshold_for_confidence(0.75, False, autopilot_module.SignalAction.BUY) == 0.45
    assert ap._ml_gate_threshold_for_confidence(0.82, False, autopilot_module.SignalAction.BUY) == 0.40
    assert ap._ml_gate_threshold_for_confidence(0.65, False, autopilot_module.SignalAction.SELL) == 0.50


def test_signal_min_confidence_follows_action(monkeypatch):
    ap = Autopilot()

    assert ap._signal_min_confidence(autopilot_module.SignalAction.BUY) == 0.40
    assert ap._signal_min_confidence(autopilot_module.SignalAction.SELL) == 0.497


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
    monkeypatch.setattr(Autopilot, "_trend_gate", lambda *_a, **_k: asyncio.sleep(0, result=(True, "ok")), raising=True)
    monkeypatch.setattr(Autopilot, "_funding_gate", lambda *_a, **_k: asyncio.sleep(0, result=(True, "ok")), raising=True)
    monkeypatch.setattr(Autopilot, "_onchain_gate", lambda *_a, **_k: asyncio.sleep(0, result=(True, "ok")), raising=True)
    monkeypatch.setattr(Autopilot, "_buy_order_plan", _buy_plan, raising=True)
    monkeypatch.setattr(Autopilot, "_place_buy", _place_buy, raising=True)
    monkeypatch.setattr(autopilot_module.RiskManager, "evaluate_entry", lambda *_a, **_k: __import__("types").SimpleNamespace(allow=True, reason="ok", notional_usdt=Decimal("50")), raising=True)

    class _Sig:
        action = autopilot_module.SignalAction.BUY
        confidence = 0.80
        contributing_agents = ["test"]
        timeframe = autopilot_module.Timeframe.D1

    await ap._execute({"BTCUSDT": _Sig()}, allow_buys=True)

    assert placed == [("BTCUSDT", Decimal("50"))]
    assert kv_state["pyramid_adds:live:BTCUSDT"] == 2


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


async def test_buy_trace_persists_market_gate_and_sizing(monkeypatch):
    ap = Autopilot()
    ap.state.mode = "live"

    class _Settings:
        min_signal_confidence = 0.55
        dynamic_threshold_enabled = False
        ml_gate_enabled = False
        buy_cooldown_minutes = 30
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
    assert info["sizing"]["rounded_qty"] == "0.1200"
    assert info["sizing"]["notional"] == "6.0000"
    assert info["final_reason"] == "market_gate"
    assert info["submitted"] is False
