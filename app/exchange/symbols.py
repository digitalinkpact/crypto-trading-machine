import asyncio
import time
import httpx
from typing import List
from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)

_SYMBOLS_CACHE = {
    'symbols': None,
    'timestamp': 0
}

async def fetch_dynamic_symbols() -> List[str]:
    """
    Fetch all tradable USDT pairs from Binance.US, filter for status=TRADING.
    Cache for SYMBOLS_CACHE_MINUTES (default 60 min).
    Fallback to static list if API fails.
    """
    s = get_settings()
    cache_minutes = getattr(s, 'symbols_cache_minutes', 60)
    now = time.time()
    if _SYMBOLS_CACHE['symbols'] and (now - _SYMBOLS_CACHE['timestamp'] < cache_minutes * 60):
        return _SYMBOLS_CACHE['symbols']
    url = "https://api.binance.us/api/v3/exchangeInfo"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            symbols = [
                s['symbol'] for s in data['symbols']
                if s['symbol'].endswith('USDT') and s['status'] == 'TRADING'
            ]
            _SYMBOLS_CACHE['symbols'] = symbols
            _SYMBOLS_CACHE['timestamp'] = now
            return symbols
    except Exception as exc:
        log.warning(f"Dynamic symbol fetch failed: {exc}. Falling back to static list.")
        # Fallback to static list from config
        return list(getattr(s, 'static_symbols', []))
