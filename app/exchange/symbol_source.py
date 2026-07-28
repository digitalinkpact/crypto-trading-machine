from app.config import get_settings
from app.exchange.symbols import fetch_dynamic_symbols, fetch_liquid_universe


def _apply_blocklist(symbols, blocked):
    if not blocked:
        return symbols
    blocked_set = {b.upper() for b in blocked}
    return [s for s in symbols if s.upper() not in blocked_set]


async def get_symbols():
    s = get_settings()
    blocked = getattr(s, 'blocked_symbols', ())
    if getattr(s, 'liquidity_pairlist_enabled', False):
        return _apply_blocklist(await fetch_liquid_universe(), blocked)
    if getattr(s, 'use_dynamic_symbols', False):
        return _apply_blocklist(await fetch_dynamic_symbols(), blocked)
    return _apply_blocklist(list(getattr(s, 'static_symbols', [])), blocked)
