from decimal import Decimal
from typing import List, Dict, Any
from app.config import get_settings

async def check_global_exposure(positions: List[Dict[str, Any]], total_balance: Decimal) -> bool:
    """
    Returns True if global exposure is within limit, False if blocked.
    """
    s = get_settings()
    max_pct = Decimal(str(s.max_long_exposure_pct)) * 100
    exposure = sum(Decimal(str(p.get('value_usdt', 0))) for p in positions)
    if total_balance <= 0:
        return False
    exposure_pct = (exposure / total_balance) * 100
    return exposure_pct <= max_pct

async def check_per_coin_concentration(proposed_value: Decimal, total_balance: Decimal) -> bool:
    """
    Returns True if proposed position is within per-coin cap, False if blocked.
    """
    s = get_settings()
    max_pct = Decimal(str(s.max_position_pct)) * 100
    if total_balance <= 0:
        return False
    pct = (proposed_value / total_balance) * 100
    return pct <= max_pct

async def check_position_count(open_positions: int, pending_orders: int) -> bool:
    """
    Returns True if position count is within limit, False if blocked.
    """
    s = get_settings()
    max_count = getattr(s, 'max_open_positions', 6)
    return (open_positions + pending_orders) < max_count
