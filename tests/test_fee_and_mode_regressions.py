"""Phase-1 measurement regressions (app/trading/autopilot.py, startup.py).

1. Live SELL fee accounting: the real exit commission is only forwarded to
   storage.reduce_position when the position's entry fee is ALSO known;
   otherwise None is passed so both legs are modeled (the entry leg used to
   be silently dropped).
2. Every storage.get_position / close_position call in the trading loops
   carries mode=self.state.mode so paper and live books can never cross.
3. Startup seeds the mutable safety-state kv rows as inactive on a fresh DB
   so _final_live_buy_safety_check does not reject every BUY with
   safety_state_unknown — while never overwriting an existing halt.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

import app.trading.autopilot as autopilot_module
from app.exchange import Order, OrderSide, OrderStatus, OrderType
from app.trading.autopilot import Autopilot


def _async_result(value):
    async def result():
        return value
    return result()


class _FakeLiveClient:
    """Fills a SELL immediately with a known USDT commission."""

    def __init__(self, commission: Decimal | None, asset: str | None = "USDT"):
        self._commission = commission
        self._asset = asset

    def generate_client_order_id(self) -> str:
        return "coid-sell"

    async def place_order(self, **kwargs):
        return Order(
            symbol=kwargs["symbol"], side=kwargs["side"], type=OrderType.MARKET,
            quantity=kwargs["quantity"], client_order_id=kwargs["client_order_id"],
            status=OrderStatus.FILLED, filled_quantity=kwargs["quantity"],
            avg_fill_price=Decimal("110"),
            commission=self._commission, commission_asset=self._asset,
        )


def _wire_live_sell(monkeypatch, *, position: dict, commission):
    ap = Autopilot()
    ap.state.mode = "live"
    monkeypatch.setattr(autopilot_module, "BinanceUSClient", lambda: _FakeLiveClient(commission))
    monkeypatch.setattr(autopilot_module.storage, "try_acquire_lock", lambda *a, **k: True)
    monkeypatch.setattr(autopilot_module.storage, "release_lock", lambda *a, **k: None)
    monkeypatch.setattr(autopilot_module.storage, "record_order", lambda **k: None)
    monkeypatch.setattr(autopilot_module.risk, "infer_exit_reason", lambda agents: "take_profit")
    monkeypatch.setattr(autopilot_module.risk, "mfe_mae_pct", lambda s, e: (None, None))

    get_position_calls: list = []
    reduce_calls: list = []

    def _get_position(symbol, mode=None):
        get_position_calls.append((symbol, mode))
        return position

    monkeypatch.setattr(autopilot_module.storage, "get_position", _get_position)
    monkeypatch.setattr(
        autopilot_module.storage, "reduce_position",
        lambda **k: reduce_calls.append(k) or {"pnl": 0},
    )
    return ap, get_position_calls, reduce_calls


async def test_live_sell_without_recorded_entry_fee_passes_none_exit_fee(monkeypatch):
    """Regression: entry_fee was computed but unused, so the exit commission
    was booked as the whole fee and the entry leg vanished from PnL."""
    pos = {"symbol": "ETHUSDT", "mode": "live", "qty": 1, "entry_price": 100, "entry_fee_usdt": None}
    ap, gets, reduces = _wire_live_sell(monkeypatch, position=pos, commission=Decimal("0.05"))

    order = await ap._submit("ETHUSDT", OrderSide.SELL, Decimal("1"), ["risk:take_profit"])

    assert order is not None and order.status == OrderStatus.FILLED
    assert len(reduces) == 1
    assert reduces[0]["actual_exit_fee_usdt"] is None
    assert reduces[0]["mode"] == "live"
    assert gets == [("ETHUSDT", "live")]


async def test_live_sell_with_recorded_entry_fee_forwards_real_exit_fee(monkeypatch):
    pos = {"symbol": "ETHUSDT", "mode": "live", "qty": 1, "entry_price": 100, "entry_fee_usdt": 0.04}
    ap, _gets, reduces = _wire_live_sell(monkeypatch, position=pos, commission=Decimal("0.05"))

    await ap._submit("ETHUSDT", OrderSide.SELL, Decimal("1"), ["risk:take_profit"])

    assert len(reduces) == 1
    assert reduces[0]["actual_exit_fee_usdt"] == 0.05


async def test_live_sell_non_usdt_commission_is_not_forwarded(monkeypatch):
    pos = {"symbol": "ETHUSDT", "mode": "live", "qty": 1, "entry_price": 100, "entry_fee_usdt": 0.04}
    ap = Autopilot()
    ap.state.mode = "live"
    monkeypatch.setattr(
        autopilot_module, "BinanceUSClient",
        lambda: _FakeLiveClient(Decimal("0.001"), asset="BNB"),
    )
    monkeypatch.setattr(autopilot_module.storage, "try_acquire_lock", lambda *a, **k: True)
    monkeypatch.setattr(autopilot_module.storage, "release_lock", lambda *a, **k: None)
    monkeypatch.setattr(autopilot_module.storage, "record_order", lambda **k: None)
    monkeypatch.setattr(autopilot_module.storage, "get_position", lambda s, mode=None: pos)
    monkeypatch.setattr(autopilot_module.risk, "infer_exit_reason", lambda agents: "stop_loss")
    monkeypatch.setattr(autopilot_module.risk, "mfe_mae_pct", lambda s, e: (None, None))
    reduces: list = []
    monkeypatch.setattr(
        autopilot_module.storage, "reduce_position", lambda **k: reduces.append(k) or {"pnl": 0},
    )

    await ap._submit("ETHUSDT", OrderSide.SELL, Decimal("1"), ["risk:stop_loss"])

    assert reduces[0]["actual_exit_fee_usdt"] is None


async def test_risk_gate_stale_dust_close_carries_mode(monkeypatch):
    """_run_risk_gates' phantom-position cleanup must look up and close the
    book row for THIS mode only."""
    from app.trading.risk import ExitDecision

    ap = Autopilot()
    ap.state.mode = "paper"
    pos = {"symbol": "XLMUSDT", "mode": "paper", "qty": 45, "entry_price": 0.10, "entry_ts": "t1"}
    monkeypatch.setattr(autopilot_module.storage, "all_positions", lambda: [pos])

    async def _snap(*, mode):
        return {"free_balances": {}, "all_balances": {}}

    monkeypatch.setattr(autopilot_module, "portfolio_snapshot", _snap)
    monkeypatch.setattr(
        Autopilot, "_price", lambda self, s: asyncio.sleep(0, result=Decimal("0.09")), raising=True,
    )
    monkeypatch.setattr(Autopilot, "_atr_pct", lambda self, s: asyncio.sleep(0, result=None), raising=True)
    monkeypatch.setattr(Autopilot, "_stop_allowed", lambda self, s: False, raising=True)
    monkeypatch.setattr(
        autopilot_module.risk, "evaluate_exits",
        lambda **k: [ExitDecision("XLMUSDT", Decimal("45"), "stop_loss")],
    )
    for fn in ("clear_hwm", "clear_tp1", "clear_tp2"):
        monkeypatch.setattr(autopilot_module.risk, fn, lambda *a, **k: None)
    monkeypatch.setattr(autopilot_module.filters, "round_qty", lambda sym, q: q)
    monkeypatch.setattr(autopilot_module.filters, "meets_min", lambda sym, q, p: False)
    monkeypatch.setattr(autopilot_module.storage, "record_tick_audit", lambda **k: None)

    gets: list = []
    closes: list = []
    monkeypatch.setattr(
        autopilot_module.storage, "get_position",
        lambda s, mode=None: gets.append((s, mode)) or pos,
    )
    monkeypatch.setattr(
        autopilot_module.storage, "close_position", lambda **k: closes.append(k) or {"pnl": 0},
    )
    monkeypatch.setattr(
        Autopilot, "_submit",
        lambda *a, **k: pytest.fail("no SELL may be submitted for a zero-balance book row"),
        raising=True,
    )

    await ap._run_risk_gates()

    assert gets == [("XLMUSDT", "paper")]
    assert len(closes) == 1
    assert closes[0]["mode"] == "paper"
    assert closes[0]["exit_reason"] == "stop_loss_stale_dust"
    assert closes[0]["exit_price"] == Decimal("0.10")  # booked at entry: PnL ~0


# ── startup seeding of safety state ───────────────────────────────────────


def _patch_kv(monkeypatch, kv_state: dict):
    from app.trading import startup

    monkeypatch.setattr(startup.storage, "kv_get", lambda key: kv_state.get(key))
    monkeypatch.setattr(startup.storage, "kv_set", lambda key, value: kv_state.__setitem__(key, value))
    return startup


def test_fresh_db_seeds_all_safety_keys_inactive(monkeypatch):
    kv_state: dict = {}
    startup = _patch_kv(monkeypatch, kv_state)

    seeded = startup.ensure_safety_state_initialized()

    assert sorted(seeded) == sorted(startup.SAFETY_STATE_KEYS)
    for key in ("drawdown_halt", "emergency_halt", "tick_protection"):
        assert kv_state[key]["active"] is False
    assert kv_state["entry_status"]["entry_halted"] is False
    assert kv_state["entry_status"]["reasons"] == []


def test_seeding_never_overwrites_an_existing_halt(monkeypatch):
    active = {"active": True, "reason": "drawdown breaker", "level": "new_entries_blocked"}
    kv_state = {
        "drawdown_halt": dict(active),
        "emergency_halt": dict(active),
        "tick_protection": {"active": False, "cleared_at": "x"},
        "entry_status": {"entry_halted": True, "reasons": ["drawdown_circuit_breaker"]},
    }
    before = {k: dict(v) for k, v in kv_state.items()}
    startup = _patch_kv(monkeypatch, kv_state)

    seeded = startup.ensure_safety_state_initialized()

    assert seeded == []
    assert kv_state == before


async def test_verify_before_trading_seeds_even_when_it_returns_early(monkeypatch):
    kv_state = {"order_outcome_unknown": {"client_order_id": "ctm-unknown"}}
    startup = _patch_kv(monkeypatch, kv_state)
    monkeypatch.setattr(startup, "get_settings", lambda: type("S", (), {"paper_trading": False})())
    monkeypatch.setattr(
        startup.watchdog, "trigger_emergency_halt", lambda reason, *, level: None,
    )
    monkeypatch.setattr(
        startup, "reconcile_positions",
        lambda **kwargs: pytest.fail("must return before reconciliation"),
    )

    await startup.verify_before_trading()

    for key in startup.SAFETY_STATE_KEYS:
        assert isinstance(kv_state[key], dict)


async def test_final_live_buy_check_passes_safety_state_after_seeding(monkeypatch):
    """End-to-end: on a fresh DB the seeded rows must satisfy the kv section
    of _final_live_buy_safety_check (it used to fail safety_state_unknown)."""
    kv_state: dict = {}
    startup = _patch_kv(monkeypatch, kv_state)
    startup.ensure_safety_state_initialized()

    ap = Autopilot()
    ap.state.mode = "live"
    ap.state.running = True
    ap.state.starting_balance_usdt = Decimal("100")

    class _Settings:
        live_buys_enabled = True

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _Settings())
    monkeypatch.setattr(autopilot_module.filters, "_loaded", True)
    monkeypatch.setattr(autopilot_module.filters, "is_listed", lambda s: True)
    monkeypatch.setattr(autopilot_module.filters, "meets_min", lambda s, q, p: True)
    monkeypatch.setattr(autopilot_module.storage, "kv_get", lambda key: kv_state.get(key))
    monkeypatch.setattr(Autopilot, "_price", lambda self, s: _async_result(Decimal("100")), raising=True)
    monkeypatch.setattr(
        autopilot_module, "portfolio_snapshot",
        lambda *, mode: _async_result({"usdt_cash": "1000", "total_usdt": "1000"}),
    )

    allowed, reason, _detail = await ap._final_live_buy_safety_check(
        symbol="BTCUSDT", qty=Decimal("1")
    )

    assert (allowed, reason) == (True, "ok")


async def test_final_live_buy_check_still_rejects_when_state_missing(monkeypatch):
    """The fail-closed default is unchanged: no seeding → safety_state_unknown."""
    ap = Autopilot()
    ap.state.mode = "live"
    ap.state.running = True
    ap.state.starting_balance_usdt = Decimal("100")

    class _Settings:
        live_buys_enabled = True

    monkeypatch.setattr(autopilot_module, "get_settings", lambda: _Settings())
    monkeypatch.setattr(autopilot_module.filters, "_loaded", True)
    monkeypatch.setattr(autopilot_module.storage, "kv_get", lambda key: None)

    allowed, reason, _detail = await ap._final_live_buy_safety_check(
        symbol="BTCUSDT", qty=Decimal("1")
    )

    assert allowed is False
    assert reason == "safety_state_unknown:drawdown_halt"
