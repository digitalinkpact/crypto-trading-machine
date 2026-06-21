"""Autopilot position-slot accounting tests."""
from __future__ import annotations

from decimal import Decimal

import pandas as pd

import app.data as data_module
import app.ta as ta_module
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
