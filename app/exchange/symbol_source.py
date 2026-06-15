from app.config import get_settings
from app.exchange.symbols import fetch_dynamic_symbols, fetch_liquid_universe

async def get_symbols():
    s = get_settings()
    if getattr(s, 'liquidity_pairlist_enabled', False):
        return await fetch_liquid_universe()
    if getattr(s, 'use_dynamic_symbols', False):
        return await fetch_dynamic_symbols()
    return list(getattr(s, 'static_symbols', []))
