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


def _async_result(value):
    async def result():
        return value
    return result()