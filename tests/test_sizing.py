from decimal import Decimal

from app.sizing import kelly_fraction, position_size


def test_kelly_basic():
    # p=0.6, b=2 → f* = 0.6 - 0.4/2 = 0.4
    assert abs(kelly_fraction(0.6, 2.0) - 0.4) < 1e-9


def test_kelly_negative_clamped():
    assert kelly_fraction(0.4, 1.0) == 0.0  # negative edge → 0


def test_kelly_invalid_inputs():
    assert kelly_fraction(0.0, 2.0) == 0.0
    assert kelly_fraction(1.0, 2.0) == 0.0
    assert kelly_fraction(0.5, 0.0) == 0.0


def test_position_size_capped():
    qty = position_size(Decimal("10000"), Decimal("100"), 0.99, 100.0)
    # f* would be huge but settings cap kicks in (default 0.10 max_position_pct)
    assert qty <= Decimal("10000") * Decimal("0.25") / Decimal("100")
