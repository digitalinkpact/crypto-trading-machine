"""Unit tests for the exchange-native protective-stop manager (phase b logic).
All deterministic — FakeExchange, no network."""
from __future__ import annotations

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

