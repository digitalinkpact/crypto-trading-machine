"""Unit tests for the exchange-native protective-stop manager (phase b logic).
All deterministic — FakeExchange, no network."""
from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from app.trading.protective_stops import (
    ProtectiveStopManager,
    StopPlan,
    backstop_price,
    stop_coid,
)
from tests.fakes import FakeExchange


class _S:
    exchange_native_stops_enabled = True
    exchange_stop_coid_prefix = "pstop-"
    exchange_stop_buffer_pct = 0.01
    exchange_stop_ratchet_min_step_pct = 0.01
    exchange_native_stops_canary_symbols = "all"
    trailing_activation_pct = 0.05
    trailing_stop_pct = 0.03
    paper_trading = False


def _mgr(client):
    return ProtectiveStopManager(client, settings=_S())


def test_backstop_price_sits_below_software_stop():
    # 1% buffer below a 100 software stop -> 99.
    assert backstop_price(Decimal("100"), 0.01) == Decimal("99.00")


def test_stop_coid_deterministic_prefixed_and_bounded():
    a = stop_coid("pstop-", "BTCUSDT", "2026-10-01T00:00:00+00:00")
    b = stop_coid("pstop-", "BTCUSDT", "2026-10-01T00:00:00+00:00")
    c = stop_coid("pstop-", "BTCUSDT", "2026-10-02T00:00:00+00:00")
    assert a == b and a != c
    assert a.startswith("pstop-") and len(a) <= 36


def test_feasible_blocks_below_min_notional_and_bad_inputs():
    m = _mgr(FakeExchange())
    plan = m.plan("BTCUSDT", Decimal("0.01"), Decimal("100"), "t")  # notional 0.99
    ok, why = ProtectiveStopManager.feasible(plan, min_notional=Decimal("10"))
    assert ok is False and "min_notional" in why
    big = m.plan("BTCUSDT", Decimal("1"), Decimal("100"), "t")  # notional 99
    ok, _ = ProtectiveStopManager.feasible(big, min_notional=Decimal("10"))
    assert ok is True
    ok, why = ProtectiveStopManager.feasible(
        StopPlan("X", Decimal("0"), Decimal("1"), "c"), min_notional=Decimal("0")
    )
    assert ok is False and why == "stop_qty_zero"


@pytest.mark.asyncio
async def test_place_records_stop_with_deterministic_coid():
    fx = FakeExchange()
    m = _mgr(fx)
    plan = m.plan("BTCUSDT", Decimal("0.01"), Decimal("100"), "t1")
    order = await m.place(plan)
    assert order is not None
    assert len(fx.open) == 1
    assert plan.coid in fx.open
    assert fx.open[plan.coid]["stopPrice"] == "99.00"


@pytest.mark.asyncio
async def test_place_failure_returns_none_so_caller_can_block_or_alert():
    fx = FakeExchange()
    fx.fail_place = True
    m = _mgr(fx)
    assert await m.place(m.plan("BTCUSDT", Decimal("0.01"), Decimal("100"), "t")) is None


@pytest.mark.asyncio
async def test_cancel_returns_canceled_then_absent_on_race():
    fx = FakeExchange()
    m = _mgr(fx)
    await m.place(m.plan("BTCUSDT", Decimal("0.01"), Decimal("100"), "t1"))
    assert await m.cancel("BTCUSDT", "t1") == "canceled"
    # Second cancel: nothing resting -> 'absent' (the cancel-then-sell race).
    assert await m.cancel("BTCUSDT", "t1") == "absent"


@pytest.mark.asyncio
async def test_cancel_absent_when_stop_already_fired():
    fx = FakeExchange()
    m = _mgr(fx)
    plan = m.plan("BTCUSDT", Decimal("0.01"), Decimal("100"), "t1")
    await m.place(plan)
    fx.trigger(plan.coid, Decimal("98"))  # stop fired before our cancel
    assert await m.cancel("BTCUSDT", "t1") == "absent"


@pytest.mark.asyncio
async def test_replace_swaps_stop_for_remaining_qty():
    fx = FakeExchange()
    m = _mgr(fx)
    await m.place(m.plan("BTCUSDT", Decimal("0.02"), Decimal("100"), "t1"))
    # After TP1 sold half -> replace for remaining 0.01 at a higher stop.
    await m.replace("BTCUSDT", Decimal("0.01"), Decimal("102"), "t1")
    assert len(fx.open) == 1
    (raw,) = fx.open.values()
    assert raw["origQty"] == "0.01"
    assert raw["stopPrice"] == "100.98"  # 102 * 0.99


@pytest.mark.asyncio
async def test_ratchet_moves_up_only():
    fx = FakeExchange()
    m = _mgr(fx)
    await m.place(m.plan("BTCUSDT", Decimal("0.01"), Decimal("100"), "t1"))  # stop 99
    # Lower software stop -> ratchet is a no-op (never move the stop down).
    assert await m.ratchet("BTCUSDT", Decimal("0.01"), Decimal("95"), "t1") is None
    (raw,) = fx.open.values()
    assert raw["stopPrice"] == "99.00"
    # Higher software stop -> ratchet moves it up.
    assert await m.ratchet("BTCUSDT", Decimal("0.01"), Decimal("110"), "t1") is not None
    (raw,) = fx.open.values()
    assert raw["stopPrice"] == "108.90"  # 110 * 0.99


@pytest.mark.asyncio
async def test_reconcile_places_missing_keeps_existing_cancels_orphans_only_ours():
    fx = FakeExchange()
    m = _mgr(fx)
    # One existing bot stop for BTC, one ORPHAN bot stop for ETH, and a
    # NON-bot order that must never be touched.
    btc = m.plan("BTCUSDT", Decimal("0.01"), Decimal("100"), "tb")
    await m.place(btc)
    eth_orphan = stop_coid("pstop-", "ETHUSDT", "old")
    fx.inject_open("ETHUSDT", eth_orphan)
    fx.inject_open("SOLUSDT", "user-manual-order-123")  # not our prefix

    desired = {
        "BTCUSDT": btc,  # already exists -> kept
        "SOLUSDT": m.plan("SOLUSDT", Decimal("1"), Decimal("20"), "ts"),  # missing -> placed
    }
    summary = await m.reconcile(desired)

    assert summary["kept"] == ["BTCUSDT"]
    assert summary["placed"] == ["SOLUSDT"]
    assert eth_orphan in summary["canceled_orphans"]
    # The non-bot order is untouched and the ETH orphan is gone.
    assert "user-manual-order-123" in fx.open
    assert eth_orphan not in fx.open


# ── autopilot integration (flag-guarded) ─────────────────────────────────

@pytest.mark.asyncio
async def test_autopilot_place_protective_stop_noop_when_disabled(monkeypatch):
    import app.trading.autopilot as ap_mod
    from app.trading.autopilot import Autopilot

    ap = Autopilot()
    ap.state.mode = "live"

    class _Off:
        exchange_native_stops_enabled = False

    monkeypatch.setattr(ap_mod, "get_settings", lambda: _Off())
    constructed = []
    monkeypatch.setattr(Autopilot, "_protective_stop_manager",
                        lambda self: constructed.append(1), raising=True)
    ok = await ap._place_protective_stop("BTCUSDT", Decimal("0.01"), Decimal("100"), "t")
    assert ok is True and constructed == []  # never even built a client


@pytest.mark.asyncio
async def test_autopilot_place_protective_stop_places_when_enabled(monkeypatch):
    import app.trading.autopilot as ap_mod
    from app.trading.autopilot import Autopilot

    ap = Autopilot()
    ap.state.mode = "live"
    monkeypatch.setattr(ap_mod, "get_settings", lambda: _S())

    fx = FakeExchange()
    monkeypatch.setattr(Autopilot, "_protective_stop_manager",
                        lambda self: ProtectiveStopManager(fx, settings=_S()), raising=True)

    async def _ssp(self, symbol, entry_price):
        return entry_price * Decimal("0.96")

    monkeypatch.setattr(Autopilot, "_software_stop_price", _ssp, raising=True)
    ok = await ap._place_protective_stop("BTCUSDT", Decimal("0.01"), Decimal("100"), "t1")
    assert ok is True
    assert len(fx.open) == 1


@pytest.mark.asyncio
async def test_autopilot_place_protective_stop_halts_on_failure(monkeypatch):
    import app.trading.autopilot as ap_mod
    import app.trading.watchdog as wd
    from app.trading.autopilot import Autopilot

    ap = Autopilot()
    ap.state.mode = "live"
    monkeypatch.setattr(ap_mod, "get_settings", lambda: _S())

    fx = FakeExchange()
    fx.fail_place = True
    monkeypatch.setattr(Autopilot, "_protective_stop_manager",
                        lambda self: ProtectiveStopManager(fx, settings=_S()), raising=True)

    async def _ssp(self, symbol, entry_price):
        return entry_price * Decimal("0.96")

    monkeypatch.setattr(Autopilot, "_software_stop_price", _ssp, raising=True)
    halts = []
    monkeypatch.setattr(wd, "trigger_emergency_halt", lambda *a, **k: halts.append(1))

    ok = await ap._place_protective_stop("BTCUSDT", Decimal("0.01"), Decimal("100"), "t1")
    assert ok is False and halts == [1]  # unprotected -> halted new entries


# ── locked-balance scenarios (i)-(viii): fail on the old behaviour ───────────

def _wire_risk_gates(monkeypatch, fake, *, position, exits, order_status="FILLED"):
    """Mock everything _run_risk_gates needs; protective-stop ops run for real
    against the FakeExchange (that's what we're testing)."""
    import app.trading.autopilot as ap_mod
    from app.exchange.models import Order, OrderStatus, OrderType
    from app.trading.autopilot import Autopilot

    ap = Autopilot()
    ap.state.mode = "live"
    base = position["symbol"][:-4]
    monkeypatch.setattr(ap_mod, "get_settings", lambda: _S())
    monkeypatch.setattr(Autopilot, "_protective_stop_manager",
                        lambda self: ProtectiveStopManager(fake, settings=_S()), raising=True)
    monkeypatch.setattr(ap_mod.storage, "all_positions", lambda: [position])
    monkeypatch.setattr(ap_mod.storage, "get_position", lambda s, mode=None: position)

    async def _snap(*, mode):
        return {"free_balances": {base: fake.free(base)},
                "all_balances": {base: fake.free(base) + fake.locked(base)}}

    monkeypatch.setattr(ap_mod, "portfolio_snapshot", _snap)
    monkeypatch.setattr(Autopilot, "_price", lambda self, s: asyncio.sleep(0, result=Decimal("100")), raising=True)
    monkeypatch.setattr(Autopilot, "_atr_pct", lambda self, s: asyncio.sleep(0, result=0.04), raising=True)
    monkeypatch.setattr(ap_mod.risk, "evaluate_exits", lambda **k: exits)
    monkeypatch.setattr(ap_mod.risk, "get_hwm", lambda s: None)
    monkeypatch.setattr(ap_mod.risk, "mfe_mae_pct", lambda s, e: (None, None))
    for fn in ("mark_tp1_taken", "mark_tp2_taken", "clear_hwm", "clear_tp1", "clear_tp2"):
        monkeypatch.setattr(ap_mod.risk, fn, lambda *a, **k: None)
    monkeypatch.setattr(ap_mod.filters, "round_qty", lambda sym, q: q)
    monkeypatch.setattr(ap_mod.filters, "meets_min", lambda sym, q, p: True)
    monkeypatch.setattr(ap_mod.storage, "record_tick_audit", lambda **k: None)

    sells: list = []
    closes: list = []

    async def _submit(self, symbol, side, qty, agents, **k):
        sells.append((symbol, side, qty))
        return Order(symbol=symbol, side=side, type=OrderType.MARKET, quantity=qty,
                     client_order_id="x", status=OrderStatus(order_status), filled_quantity=qty)

    monkeypatch.setattr(Autopilot, "_submit", _submit, raising=True)
    monkeypatch.setattr(ap_mod.storage, "close_position", lambda **k: closes.append(k) or {"pnl": 0})
    monkeypatch.setattr(ap_mod.storage, "reduce_position", lambda **k: None)
    return ap, sells, closes


@pytest.mark.asyncio
async def test_i_resting_stop_not_stale_closed_and_ii_cancel_then_sell(monkeypatch):
    fake = FakeExchange()
    fake.seed_balance("ADA", 100)
    mgr = ProtectiveStopManager(fake, settings=_S())
    await mgr.place(mgr.plan("ADAUSDT", Decimal("100"), Decimal("0.96"), "t1"))
    assert fake.free("ADA") == 0 and fake.locked("ADA") == 100   # stop locks the balance
    coid = stop_coid("pstop-", "ADAUSDT", "t1")
    from app.trading.risk import ExitDecision
    pos = {"symbol": "ADAUSDT", "mode": "live", "qty": 100, "entry_price": 1.0, "entry_ts": "t1"}
    ap, sells, closes = _wire_risk_gates(
        monkeypatch, fake, position=pos, exits=[ExitDecision("ADAUSDT", Decimal("100"), "stop_loss")])
    await ap._run_risk_gates()
    assert coid in fake.canceled                      # (ii) stop cancelled first
    assert len(sells) == 1 and sells[0][2] == Decimal("100")  # sold the freed full qty
    assert not any("stale" in (c.get("exit_reason") or "") for c in closes)  # (i) NOT stale-closed


@pytest.mark.asyncio
async def test_iii_exchange_stop_fill_mid_flight_no_double_sell(monkeypatch):
    fake = FakeExchange()
    fake.seed_balance("ADA", 100)
    mgr = ProtectiveStopManager(fake, settings=_S())
    await mgr.place(mgr.plan("ADAUSDT", Decimal("100"), Decimal("0.96"), "t1"))
    fake.trigger(stop_coid("pstop-", "ADAUSDT", "t1"), Decimal("0.95"))  # stop fired on exchange
    from app.trading.risk import ExitDecision
    pos = {"symbol": "ADAUSDT", "mode": "live", "qty": 100, "entry_price": 1.0, "entry_ts": "t1"}
    ap, sells, closes = _wire_risk_gates(
        monkeypatch, fake, position=pos, exits=[ExitDecision("ADAUSDT", Decimal("100"), "stop_loss")])
    await ap._run_risk_gates()
    assert sells == []                                 # no double-sell
    assert len(closes) == 1
    assert closes[0]["exit_reason"] == "exchange_stop"
    assert closes[0]["exit_price"] == Decimal("0.95")  # recorded at the ACTUAL fill price


@pytest.mark.asyncio
async def test_iv_tp1_partial_replaces_stop_for_remaining(monkeypatch):
    fake = FakeExchange()
    fake.seed_balance("ADA", 100)
    mgr = ProtectiveStopManager(fake, settings=_S())
    await mgr.place(mgr.plan("ADAUSDT", Decimal("100"), Decimal("0.96"), "t1"))
    from app.trading.risk import ExitDecision
    # after TP1 sells 50, get_position reports the remaining 50
    pos = {"symbol": "ADAUSDT", "mode": "live", "qty": 50, "entry_price": 1.0, "entry_ts": "t1"}
    ap, sells, closes = _wire_risk_gates(
        monkeypatch, fake, position=pos, exits=[ExitDecision("ADAUSDT", Decimal("50"), "take_profit_1")])
    await ap._run_risk_gates()
    resting = list(fake.open.values())
    assert len(resting) == 1 and resting[0]["origQty"] == "50"  # stop re-placed for remaining qty


@pytest.mark.asyncio
async def test_v_restart_does_not_duplicate_resting_stop(monkeypatch):
    import app.trading.autopilot as ap_mod
    from app.trading.autopilot import Autopilot
    fake = FakeExchange()
    fake.seed_balance("ADA", 100)
    mgr = ProtectiveStopManager(fake, settings=_S())
    await mgr.place(mgr.plan("ADAUSDT", Decimal("100"), Decimal("0.96"), "t1"))
    assert len(fake.open) == 1
    pos = {"symbol": "ADAUSDT", "mode": "live", "qty": 100, "entry_price": 1.0, "entry_ts": "t1"}
    ap = Autopilot()
    ap.state.mode = "live"
    monkeypatch.setattr(ap_mod, "get_settings", lambda: _S())
    monkeypatch.setattr(Autopilot, "_protective_stop_manager",
                        lambda self: ProtectiveStopManager(fake, settings=_S()), raising=True)
    monkeypatch.setattr(ap_mod.storage, "all_positions", lambda: [pos])
    monkeypatch.setattr(Autopilot, "_atr_pct", lambda self, s: asyncio.sleep(0, result=0.04), raising=True)
    monkeypatch.setattr(ap_mod.risk, "get_hwm", lambda s: None)
    await ap.reconcile_protective_stops()
    assert len(fake.open) == 1   # existing stop kept, not duplicated


@pytest.mark.asyncio
async def test_vi_drift_check_passes_with_locked_balance(monkeypatch):
    import app.trading.reconcile as rec
    pos = {"symbol": "ADAUSDT", "mode": "live", "qty": 100, "entry_price": 1.0, "entry_ts": "t1"}
    monkeypatch.setattr(rec.storage, "all_positions", lambda: [pos])

    async def _snap(*, mode):
        # all_balances is free+locked (total); the 100 ADA are locked in a stop.
        return {"all_balances": {"ADA": Decimal("100")}, "holdings": []}

    monkeypatch.setattr(rec, "portfolio_snapshot", _snap)
    monkeypatch.setattr(rec.storage, "close_position", lambda **k: pytest.fail("must not close a locked position"))
    result = await rec.reconcile_positions(mode="live")
    assert result.get("kept") == 1 and result.get("closed", 0) == 0


def test_vii_prebuy_feasibility_blocks_infeasible_stop(monkeypatch):
    import app.trading.autopilot as ap_mod
    from app.trading.autopilot import Autopilot
    ap = Autopilot()
    ap.state.mode = "live"
    monkeypatch.setattr(ap_mod, "get_settings", lambda: _S())
    monkeypatch.setattr(ap_mod, "BinanceUSClient", lambda *a, **k: object())
    ok, why = ap._protective_stop_feasible(
        "ADAUSDT", Decimal("0.01"), Decimal("100"), "t1",
        software_stop=Decimal("96"), min_notional=Decimal("10"))
    assert ok is False and "min_notional" in why     # 0.01*~95 < 10 -> entry blocked
    ok2, _ = ap._protective_stop_feasible(
        "ADAUSDT", Decimal("1"), Decimal("100"), "t1",
        software_stop=Decimal("96"), min_notional=Decimal("10"))
    assert ok2 is True


@pytest.mark.asyncio
async def test_viii_place_failure_halts_but_exits_still_run(monkeypatch):
    import app.trading.autopilot as ap_mod
    import app.trading.watchdog as wd
    from app.trading.autopilot import Autopilot
    ap = Autopilot()
    ap.state.mode = "live"
    monkeypatch.setattr(ap_mod, "get_settings", lambda: _S())
    fake = FakeExchange()
    fake.fail_place = True
    monkeypatch.setattr(Autopilot, "_protective_stop_manager",
                        lambda self: ProtectiveStopManager(fake, settings=_S()), raising=True)
    monkeypatch.setattr(Autopilot, "_software_stop_price",
                        lambda self, s, p: asyncio.sleep(0, result=p * Decimal("0.96")), raising=True)
    halts = []
    monkeypatch.setattr(wd, "trigger_emergency_halt", lambda *a, **k: halts.append(1))
    ok = await ap._place_protective_stop("ADAUSDT", Decimal("1"), Decimal("100"), "t1")
    assert ok is False and halts == [1]               # unprotected -> halt new entries

    # Exits still run even with the halt: a stop_loss on a plain (no-stop) holding sells.
    fake2 = FakeExchange()
    fake2.seed_balance("ADA", 100)
    from app.trading.risk import ExitDecision
    pos = {"symbol": "ADAUSDT", "mode": "live", "qty": 100, "entry_price": 1.0, "entry_ts": "t2"}
    ap2, sells, closes = _wire_risk_gates(
        monkeypatch, fake2, position=pos, exits=[ExitDecision("ADAUSDT", Decimal("100"), "stop_loss")])
    await ap2._run_risk_gates()
    assert len(sells) == 1                             # exits unaffected by the entry halt


@pytest.mark.asyncio
async def test_ix_existing_position_gets_stop_via_risk_gates(monkeypatch):
    """An ALREADY-OPEN allowlisted position must get its backstop placed by the
    normal risk tick (reconcile), not only on a fresh buy."""
    fake = FakeExchange()
    fake.seed_balance("ADA", 100)
    pos = {"symbol": "ADAUSDT", "mode": "live", "qty": 100, "entry_price": 1.0, "entry_ts": "t1"}
    ap, sells, closes = _wire_risk_gates(monkeypatch, fake, position=pos, exits=[])
    assert len(fake.open) == 0                          # no stop before the tick
    await ap._run_risk_gates()
    assert len(fake.open) == 1                          # reconcile placed the backstop
    (raw,) = fake.open.values()
    assert raw["clientOrderId"] == stop_coid("pstop-", "ADAUSDT", "t1")
    assert fake.locked("ADA") == 100 and fake.free("ADA") == 0


def test_canary_validator_requires_allowlist_when_enabled():
    from app.config import Settings
    with pytest.raises(Exception):
        Settings(_env_file=None, exchange_native_stops_enabled=True,
                 exchange_native_stops_canary_symbols="")
    s = Settings(_env_file=None, exchange_native_stops_enabled=True,
                 exchange_native_stops_canary_symbols="BTCUSDT")
    assert s.exchange_stop_allowed("BTCUSDT") and not s.exchange_stop_allowed("ETHUSDT")
    s2 = Settings(_env_file=None, exchange_native_stops_enabled=True,
                  exchange_native_stops_canary_symbols="all")
    assert s2.exchange_stop_allowed("ANYUSDT")
    s3 = Settings(_env_file=None, exchange_native_stops_enabled=False)
    assert s3.exchange_stop_allowed("BTCUSDT") is False




# ── detect_fill: honest market-stop fill price + commission ──────────────────
@pytest.mark.asyncio
async def test_detect_fill_uses_cumquote_not_zero_price_field():
    """A market STOP_LOSS reports price=0; detect_fill must price it at
    cummulativeQuoteQty/executedQty and recover the fee from myTrades."""
    fake = FakeExchange()
    coid = stop_coid("pstop-", "ADAUSDT", "t1")
    fake.seed_fill(
        coid, "ADAUSDT", executed_qty="1.5", cum_quote="45000.0", order_id=7777,
        trades=[{"orderId": 7777, "commission": "0.07", "commissionAsset": "USDT"}],
    )
    fill = await _mgr(fake).detect_fill("ADAUSDT", "t1")
    assert fill is not None
    assert fill["fill_price"] == Decimal("30000")      # 45000 / 1.5, not the 0 price field
    assert fill["fill_price"] > 0
    assert fill["fee"] == Decimal("0.07") and fill["fee_asset"] == "USDT"


@pytest.mark.asyncio
async def test_detect_fill_returns_none_when_executed_qty_zero():
    fake = FakeExchange()
    coid = stop_coid("pstop-", "ADAUSDT", "t1")
    fake.seed_fill(coid, "ADAUSDT", executed_qty="0", cum_quote="0", order_id=7778)
    assert await _mgr(fake).detect_fill("ADAUSDT", "t1") is None


@pytest.mark.asyncio
async def test_detect_fill_models_fee_when_mytrades_fails():
    """If the myTrades fee lookup fails, detect_fill still returns a valid price
    with fee=None so the caller records the exit with a MODELED fee."""
    fake = FakeExchange()
    fake.fail_my_trades = True
    coid = stop_coid("pstop-", "ADAUSDT", "t1")
    fake.seed_fill(coid, "ADAUSDT", executed_qty="2", cum_quote="60.0", order_id=7779)
    fill = await _mgr(fake).detect_fill("ADAUSDT", "t1")
    assert fill is not None
    assert fill["fill_price"] == Decimal("30")
    assert fill["fee"] is None and fill["fee_asset"] is None


@pytest.mark.asyncio
async def test_detect_fill_mixed_commission_assets_fall_back_to_modeled():
    fake = FakeExchange()
    coid = stop_coid("pstop-", "ADAUSDT", "t1")
    fake.seed_fill(
        coid, "ADAUSDT", executed_qty="2", cum_quote="60.0", order_id=7780,
        trades=[
            {"orderId": 7780, "commission": "0.01", "commissionAsset": "USDT"},
            {"orderId": 7780, "commission": "0.5", "commissionAsset": "BNB"},
        ],
    )
    fill = await _mgr(fake).detect_fill("ADAUSDT", "t1")
    assert fill is not None and fill["fill_price"] == Decimal("30")
    assert fill["fee"] is None          # mixed assets -> don't guess -> modeled downstream


@pytest.mark.asyncio
async def test_record_exit_never_closes_position_at_zero_price(monkeypatch):
    """Backstop: even a malformed fill with price 0 must NOT reach close_position."""
    import app.trading.autopilot as ap_mod
    from app.trading.autopilot import Autopilot
    ap = Autopilot()
    ap.state.mode = "live"
    closes: list = []
    monkeypatch.setattr(ap_mod.storage, "get_position", lambda s, mode=None: {"entry_price": 1.0})
    monkeypatch.setattr(ap_mod.risk, "mfe_mae_pct", lambda s, e: (None, None))
    for fn in ("clear_hwm", "clear_tp1", "clear_tp2"):
        monkeypatch.setattr(ap_mod.risk, fn, lambda *a, **k: None)
    monkeypatch.setattr(ap_mod.storage, "close_position", lambda **k: closes.append(k))
    await ap._record_exchange_stop_exit("ADAUSDT", {"fill_price": Decimal("0"), "fee": None})
    await ap._record_exchange_stop_exit("ADAUSDT", {"fill_price": None, "fee": None})
    assert closes == []                 # never recorded a 0/None-price exit


@pytest.mark.asyncio
async def test_record_exit_closes_at_real_price_with_actual_fee(monkeypatch):
    import app.trading.autopilot as ap_mod
    from app.trading.autopilot import Autopilot
    ap = Autopilot()
    ap.state.mode = "live"
    closes: list = []
    monkeypatch.setattr(ap_mod.storage, "get_position", lambda s, mode=None: {"entry_price": 1.0})
    monkeypatch.setattr(ap_mod.risk, "mfe_mae_pct", lambda s, e: (None, None))
    for fn in ("clear_hwm", "clear_tp1", "clear_tp2"):
        monkeypatch.setattr(ap_mod.risk, fn, lambda *a, **k: None)
    monkeypatch.setattr(ap_mod.storage, "close_position", lambda **k: closes.append(k))
    await ap._record_exchange_stop_exit(
        "ADAUSDT", {"fill_price": Decimal("0.95"), "fee": Decimal("0.07"), "fee_asset": "USDT"})
    assert len(closes) == 1
    assert closes[0]["exit_price"] == Decimal("0.95")
    # The stop fill's commission is the EXIT leg only — it must be passed as
    # such so storage still charges the entry leg instead of dropping it.
    assert closes[0]["actual_exit_fee_usdt"] == 0.07
    assert "actual_fee_usdt" not in closes[0]
    assert closes[0]["mode"] == "live"
