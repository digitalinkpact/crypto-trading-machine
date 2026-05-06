"""Kelly criterion + portfolio risk caps."""
from __future__ import annotations

from decimal import Decimal

from app.config import get_settings


def kelly_fraction(win_prob: float, win_loss_ratio: float) -> float:
    """Classic Kelly: f* = p - (1 - p) / b.

    Args:
        win_prob: probability of a winning trade in (0, 1).
        win_loss_ratio: average win size divided by average loss size (b).
    """
    if not (0.0 < win_prob < 1.0):
        return 0.0
    if win_loss_ratio <= 0.0:
        return 0.0
    f = win_prob - (1.0 - win_prob) / win_loss_ratio
    return max(0.0, f)


def position_size(
    equity: Decimal,
    price: Decimal,
    win_prob: float,
    win_loss_ratio: float,
) -> Decimal:
    """Position size in base-asset units, capped by risk settings."""
    if price <= 0:
        return Decimal("0")
    settings = get_settings()
    f_star = kelly_fraction(win_prob, win_loss_ratio)
    f_capped = min(f_star, settings.kelly_fraction_cap, settings.max_position_pct)
    notional = equity * Decimal(str(f_capped))
    return notional / price
