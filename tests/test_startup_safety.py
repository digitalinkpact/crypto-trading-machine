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


def _async_result(value):
    async def result():
        return value
    return result()