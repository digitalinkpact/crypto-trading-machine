"""Autopilot position-slot accounting tests."""
from __future__ import annotations

from decimal import Decimal

from app.trading import autopilot as autopilot_module
from app.trading.autopilot import Autopilot


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
