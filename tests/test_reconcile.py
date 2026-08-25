"""Tests for app/trading/reconcile.py — keeping local `positions` rows aligned
with real exchange/paper holdings in BOTH drift directions: a stale DB row the
exchange no longer backs, and (the more dangerous case) a real exchange
holding the DB has never heard of and is therefore not protecting with any
stop-loss/take-profit/trailing logic.
"""
from __future__ import annotations

from decimal import Decimal

import app.trading.reconcile as reconcile_module
from app.trading.reconcile import reconcile_positions


async def _fake_snapshot(**_kwargs):
    return {
        "all_balances": {"ZEC": Decimal("0.5"), "USDT": Decimal("10")},
        "holdings": [
            {"asset": "ZEC", "qty": Decimal("0.5"), "price_usdt": Decimal("40"), "value_usdt": Decimal("20")},
        ],
    }


async def test_reconcile_closes_stale_db_position_with_no_real_balance(monkeypatch):
    monkeypatch.setattr(reconcile_module, "portfolio_snapshot", _fake_snapshot)
    monkeypatch.setattr(
        reconcile_module.storage, "all_positions",
        lambda: [{"symbol": "SOLUSDT", "mode": "live", "qty": 1.0, "entry_price": 100.0}],
    )
    closed_calls = []
    monkeypatch.setattr(
        reconcile_module.storage, "close_position",
        lambda **kw: closed_calls.append(kw),
    )
    monkeypatch.setattr(reconcile_module.storage, "open_position", lambda **kw: None)

    result = await reconcile_positions(mode="live")

    assert result["closed"] == 1
    assert result["adopted"] == 1  # the untracked ZEC holding from the fake snapshot
    assert closed_calls[0]["symbol"] == "SOLUSDT"


async def test_reconcile_adopts_untracked_exchange_holding(monkeypatch):
    """The exact 2026-08-03 incident class: exchange holds a real position the
    DB has no row for at all. Must be auto-adopted so risk gates see it."""
    monkeypatch.setattr(reconcile_module, "portfolio_snapshot", _fake_snapshot)
    monkeypatch.setattr(reconcile_module.storage, "all_positions", lambda: [])
    opened_calls = []
    monkeypatch.setattr(
        reconcile_module.storage, "open_position",
        lambda **kw: opened_calls.append(kw),
    )

    result = await reconcile_positions(mode="live")

    assert result["adopted"] == 1
    assert opened_calls[0]["symbol"] == "ZECUSDT"
    assert opened_calls[0]["mode"] == "live"
    assert opened_calls[0]["qty"] == Decimal("0.5")
    assert opened_calls[0]["entry_price"] == Decimal("40")


async def test_reconcile_ignores_dust_below_threshold(monkeypatch):
    async def _dust_snapshot(**_kwargs):
        return {
            "all_balances": {"SHIB": Decimal("100")},
            "holdings": [
                {"asset": "SHIB", "qty": Decimal("100"), "price_usdt": Decimal("0.001"), "value_usdt": Decimal("0.10")},
            ],
        }

    monkeypatch.setattr(reconcile_module, "portfolio_snapshot", _dust_snapshot)
    monkeypatch.setattr(reconcile_module.storage, "all_positions", lambda: [])
    opened_calls = []
    monkeypatch.setattr(
        reconcile_module.storage, "open_position",
        lambda **kw: opened_calls.append(kw),
    )

    result = await reconcile_positions(mode="live")

    assert result["adopted"] == 0
    assert opened_calls == []


async def test_reconcile_ignores_stablecoin_holdings(monkeypatch):
    async def _stable_snapshot(**_kwargs):
        return {
            "all_balances": {"USDC": Decimal("50")},
            "holdings": [
                {"asset": "USDC", "qty": Decimal("50"), "price_usdt": Decimal("1"), "value_usdt": Decimal("50")},
            ],
        }

    monkeypatch.setattr(reconcile_module, "portfolio_snapshot", _stable_snapshot)
    monkeypatch.setattr(reconcile_module.storage, "all_positions", lambda: [])
    opened_calls = []
    monkeypatch.setattr(
        reconcile_module.storage, "open_position",
        lambda **kw: opened_calls.append(kw),
    )

    result = await reconcile_positions(mode="live")

    assert result["adopted"] == 0
    assert opened_calls == []


async def test_reconcile_adoption_failure_triggers_emergency_halt(monkeypatch):
    monkeypatch.setattr(reconcile_module, "portfolio_snapshot", _fake_snapshot)
    monkeypatch.setattr(reconcile_module.storage, "all_positions", lambda: [])

    def _raise(**_kw):
        raise RuntimeError("db locked")

    monkeypatch.setattr(reconcile_module.storage, "open_position", _raise)

    import app.trading.watchdog as watchdog_module
    halt_calls = []
    monkeypatch.setattr(
        watchdog_module, "trigger_emergency_halt",
        lambda reason, level="new_entries_blocked": halt_calls.append((reason, level)),
    )

    result = await reconcile_positions(mode="live")

    assert result["adopted"] == 0
    assert len(halt_calls) == 1
    assert halt_calls[0][1] == "new_entries_blocked"
