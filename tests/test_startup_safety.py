from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_startup_reconcile_blocks_entries_on_position_drift(monkeypatch):
    from app.trading import startup as main

    monkeypatch.setattr(main, "get_settings", lambda: type("S", (), {"paper_trading": False})())
    monkeypatch.setattr(main, "reconcile_positions", lambda **kwargs: _async_result({"mismatched": 1}))
    monkeypatch.setattr("app.exchange.BinanceUSClient.open_orders", lambda self: _async_result([]))
    halts = []
    monkeypatch.setattr(
        main.watchdog, "trigger_emergency_halt",
        lambda reason, *, level: halts.append((reason, level)),
    )
    monkeypatch.setattr(
        main.watchdog, "_verify_safe_to_resume",
        lambda _mode: _async_result((True, "protective stops verified")),
    )

    await main.verify_before_trading()

    assert len(halts) == 1
    assert halts[0][1] == "new_entries_blocked"


@pytest.mark.asyncio
async def test_startup_reconcile_blocks_entries_when_verification_fails(monkeypatch):
    from app.trading import startup as main

    monkeypatch.setattr(main, "get_settings", lambda: type("S", (), {"paper_trading": False})())

    async def fail(**kwargs):
        raise ConnectionError("exchange unavailable")

    monkeypatch.setattr(main, "reconcile_positions", fail)
    halts = []
    monkeypatch.setattr(
        main.watchdog, "trigger_emergency_halt",
        lambda reason, *, level: halts.append((reason, level)),
    )
    monkeypatch.setattr(
        main.watchdog, "_verify_safe_to_resume",
        lambda _mode: _async_result((False, "protective stop under-sized for APTUSDT")),
    )

    await main.verify_before_trading()

    assert len(halts) == 1
    assert "startup reconciliation failed" in halts[0][0]


@pytest.mark.asyncio
async def test_startup_blocks_entries_when_prior_order_outcome_is_unknown(monkeypatch):
    from app.trading import startup as main

    monkeypatch.setattr(main, "get_settings", lambda: type("S", (), {"paper_trading": False})())
    monkeypatch.setattr(main.storage, "kv_get", lambda key: {"client_order_id": "ctm-unknown"} if key == "order_outcome_unknown" else None)
    reconcile_called = False

    async def reconcile(**kwargs):
        nonlocal reconcile_called
        reconcile_called = True
        return {"mismatched": 0}

    monkeypatch.setattr(main, "reconcile_positions", reconcile)
    halts = []
    monkeypatch.setattr(
        main.watchdog, "trigger_emergency_halt",
        lambda reason, *, level: halts.append((reason, level)),
    )

    await main.verify_before_trading()

    assert reconcile_called is False
    assert halts and halts[0][1] == "order_outcome_unknown"


@pytest.mark.asyncio
async def test_startup_preserves_legacy_drawdown_halt(monkeypatch):
    from app.trading import startup as main

    kv_state = {
        "autopilot_state": {"last_error": "DRAWDOWN BREAKER TRIPPED at -20%"},
        "entry_status": {"reasons": ["drawdown_circuit_breaker"]},
    }
    monkeypatch.setattr(main, "get_settings", lambda: type("S", (), {"paper_trading": False})())
    monkeypatch.setattr(main.storage, "kv_get", lambda key: kv_state.get(key))
    monkeypatch.setattr(main.storage, "kv_set", lambda key, value: kv_state.__setitem__(key, value))
    monkeypatch.setattr(main, "reconcile_positions", lambda **kwargs: _async_result({"mismatched": 0}))
    monkeypatch.setattr("app.exchange.BinanceUSClient.open_orders", lambda self: _async_result([]))

    await main.verify_before_trading()

    assert kv_state["drawdown_halt"]["active"] is True
    assert kv_state["drawdown_halt"]["legacy"] is True


@pytest.mark.asyncio
async def test_startup_blocks_entries_when_running_state_has_no_baseline(monkeypatch):
    from app.trading import startup as main

    kv_state = {
        "autopilot_state": {"running": True, "started_at": "2026-08-28T00:00:00+00:00"},
    }
    monkeypatch.setattr(main, "get_settings", lambda: type("S", (), {"paper_trading": False})())
    monkeypatch.setattr(main.storage, "kv_get", lambda key: kv_state.get(key))
    monkeypatch.setattr(main.storage, "kv_set", lambda key, value: kv_state.__setitem__(key, value))
    monkeypatch.setattr(main, "reconcile_positions", lambda **kwargs: _async_result({"mismatched": 0}))
    monkeypatch.setattr("app.exchange.BinanceUSClient.open_orders", lambda self: _async_result([]))

    await main.verify_before_trading()

    assert kv_state["baseline_unavailable"]["active"] is True


@pytest.mark.asyncio
async def test_startup_accepts_expected_protective_stops(monkeypatch):
    from types import SimpleNamespace

    from app.trading import startup as main
    from app.trading.protective_stops import stop_coid

    position = {
        "symbol": "APTUSDT", "mode": "live", "qty": "33.44",
        "entry_ts": "2026-10-22T00:00:00+00:00",
    }
    coid = stop_coid("pstop-", position["symbol"], position["entry_ts"])
    settings = SimpleNamespace(
        paper_trading=False,
        exchange_native_stops_enabled=True,
        exchange_stop_coid_prefix="pstop-",
        exchange_stop_allowed=lambda _symbol: True,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "reconcile_positions", lambda **_kwargs: _async_result({"mismatched": 0}))
    monkeypatch.setattr(main.storage, "all_positions", lambda: [position])
    monkeypatch.setattr(main.watchdog, "_verify_safe_to_resume", lambda _mode: _async_result((True, "covered")))
    monkeypatch.setattr(
        "app.exchange.BinanceUSClient.open_orders",
        lambda _self: _async_result([{
            "clientOrderId": coid, "symbol": "APTUSDT", "side": "SELL",
            "type": "STOP_LOSS", "status": "NEW", "origQty": "33.43",
        }]),
    )
    halts = []
    monkeypatch.setattr(
        main.watchdog, "trigger_emergency_halt",
        lambda reason, *, level: halts.append((reason, level)),
    )

    await main.verify_before_trading()

    assert halts == []


@pytest.mark.asyncio
async def test_startup_blocks_when_canary_position_has_no_protective_stop(monkeypatch):
    from types import SimpleNamespace

    from app.trading import startup as main

    position = {
        "symbol": "APTUSDT", "mode": "live", "qty": "33.44",
        "entry_ts": "2026-10-22T00:00:00+00:00",
    }
    settings = SimpleNamespace(
        paper_trading=False,
        exchange_native_stops_enabled=True,
        exchange_stop_coid_prefix="pstop-",
        exchange_stop_allowed=lambda _symbol: True,
    )
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    monkeypatch.setattr(main, "reconcile_positions", lambda **_kwargs: _async_result({"mismatched": 0}))
    monkeypatch.setattr(main.storage, "all_positions", lambda: [position])
    monkeypatch.setattr(
        "app.exchange.BinanceUSClient.open_orders",
        lambda _self: _async_result([]),
    )
    halts = []
    monkeypatch.setattr(
        main.watchdog, "trigger_emergency_halt",
        lambda reason, *, level: halts.append((reason, level)),
    )

    await main.verify_before_trading()

    assert halts and "1 missing/inactive protective stop(s)" in halts[0][0]


def _async_result(value):
    async def result():
        return value
    return result()


@pytest.mark.asyncio
async def test_fresh_db_halt_states_seeded_so_final_live_buy_gate_can_pass(monkeypatch):
    from decimal import Decimal

    import app.trading.autopilot as autopilot_module
    from app.trading import startup as main
    from app.trading.autopilot import Autopilot

    ap = Autopilot()
    ap.state.mode = "live"
    ap.state.running = True
    ap.state.starting_balance_usdt = Decimal("100")
    monkeypatch.setattr(
        autopilot_module, "get_settings",
        lambda: type("S", (), {"live_buys_enabled": True})(),
    )
    monkeypatch.setattr(autopilot_module.filters, "_loaded", True)
    # Halted entry_status is the next check after the halt keys, so reaching
    # it proves the halt-key gate passed without touching price/portfolio.
    main.storage.kv_set("entry_status", {"entry_halted": True})

    allowed, reason, _ = await ap._final_live_buy_safety_check(symbol="BTCUSDT", qty=Decimal("1"))
    assert (allowed, reason) == (False, "safety_state_unknown:drawdown_halt")

    assert main.initialize_halt_states() == list(main.HALT_STATE_KEYS)
    allowed, reason, _ = await ap._final_live_buy_safety_check(symbol="BTCUSDT", qty=Decimal("1"))
    assert (allowed, reason) == (False, "entry_status_halted")


def test_halt_state_seeding_never_overwrites_existing_or_corrupt_rows():
    from app.trading import startup as main

    main.storage.kv_set("drawdown_halt", {"active": True, "reason": "tripped"})
    with main.storage._conn() as c:
        c.execute("INSERT INTO kv(key,value) VALUES('tick_protection','{not json')")

    assert main.initialize_halt_states() == ["emergency_halt"]
    assert main.initialize_halt_states() == []

    assert main.storage.kv_get("drawdown_halt") == {"active": True, "reason": "tripped"}
    assert main.storage.kv_get("tick_protection") is None
    assert main.storage.kv_get("emergency_halt")["active"] is False