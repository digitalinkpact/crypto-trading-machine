import time
import httpx
from typing import List

from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)

# Leveraged ETF token suffixes (e.g. BTCUPUSDT, ETHDOWNUSDT). These decay and
# are unsuitable for spot trend/mean-reversion strategies.
_LEVERAGED_SUFFIXES = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")

# Stablecoin bases — trading <stable>USDT has no edge and just bleeds fees.
_STABLE_BASES = {
    "USDC", "USDT", "DAI", "TUSD", "USDP", "PAX", "BUSD", "FDUSD",
    "USD", "UST", "USTC", "GUSD", "PYUSD", "EUR", "EURI",
}

_SYMBOLS_CACHE: dict = {"symbols": None, "timestamp": 0.0}


def _is_leveraged(symbol: str) -> bool:
    return any(symbol.endswith(suffix) for suffix in _LEVERAGED_SUFFIXES)


def _is_stable_pair(symbol: str) -> bool:
    # symbol ends with "USDT"; the base is everything before it.
    return symbol[:-4] in _STABLE_BASES


async def fetch_dynamic_symbols() -> List[str]:
    """Fetch tradable USDT pairs from Binance.US (status=TRADING).

    Excludes leveraged ETF tokens and stablecoin->stablecoin pairs. When
    `min_quote_volume_usdt > 0`, also drops pairs below that 24h quote-volume
    floor (a liquidity guard against thin-book slippage). When `max_symbols > 0`,
    caps the result to the top-N pairs ranked by 24h quote volume (most liquid).
    Cached for `symbols_cache_minutes`. Falls back to the static list on any
    API failure.
    """
    s = get_settings()
    cache_minutes = getattr(s, "symbols_cache_minutes", 60)
    now = time.time()
    if _SYMBOLS_CACHE["symbols"] and (now - _SYMBOLS_CACHE["timestamp"] < cache_minutes * 60):
        return _SYMBOLS_CACHE["symbols"]

    base_url = s.binance_base_url.rstrip("/")
    exclude_leveraged = getattr(s, "exclude_leveraged_tokens", True)
    floor = float(getattr(s, "min_quote_volume_usdt", 0.0) or 0.0)
    top_n = int(getattr(s, "max_symbols", 0) or 0)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{base_url}/api/v3/exchangeInfo")
            resp.raise_for_status()
            data = resp.json()
            symbols = [
                d["symbol"]
                for d in data["symbols"]
                if d["symbol"].endswith("USDT")
                and d["status"] == "TRADING"
                and not (exclude_leveraged and _is_leveraged(d["symbol"]))
                and not _is_stable_pair(d["symbol"])
            ]

            if floor > 0 or top_n > 0:
                t = await client.get(f"{base_url}/api/v3/ticker/24hr")
                t.raise_for_status()
                vol = {
                    row["symbol"]: float(row.get("quoteVolume", 0.0) or 0.0)
                    for row in t.json()
                }
                if floor > 0:
                    symbols = [sym for sym in symbols if vol.get(sym, 0.0) >= floor]
                if top_n > 0:
                    # Keep the top-N most-liquid pairs by 24h quote volume.
                    symbols = sorted(
                        symbols, key=lambda sym: vol.get(sym, 0.0), reverse=True
                    )[:top_n]

            symbols.sort()
            _SYMBOLS_CACHE["symbols"] = symbols
            _SYMBOLS_CACHE["timestamp"] = now
            log.info(
                "dynamic symbols: %d USDT pairs (leveraged_excluded=%s, vol_floor=%.0f, top_n=%d)",
                len(symbols), exclude_leveraged, floor, top_n,
            )
            return symbols
    except Exception as exc:  # noqa: BLE001
        log.warning("Dynamic symbol fetch failed: %s. Falling back to static list.", exc)
        return list(getattr(s, "static_symbols", []))

