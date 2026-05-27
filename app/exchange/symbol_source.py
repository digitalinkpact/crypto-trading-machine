from app.config import get_settings
from app.exchange.symbols import fetch_dynamic_symbols

async def get_symbols():
    s = get_settings()
    if getattr(s, 'use_dynamic_symbols', False):
        return await fetch_dynamic_symbols()
    return list(getattr(s, 'static_symbols', []))
