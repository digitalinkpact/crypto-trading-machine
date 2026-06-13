"""Optional on-chain whale-flow signal (exchange inflows).

When coins move *onto* exchanges, holders are usually positioning to sell — a
spike in exchange inflow is a classic bearish tell. This reader pulls 24h
exchange-inflow history from Glassnode and flags when the latest value is an
outlier (z-score) versus its trailing mean.

Requires a Glassnode API key and `onchain_enabled=true`. Everything here is
best-effort and non-fatal: with no key, disabled, or any error, `inflow_spike`
returns `(False, reason)` so the trading loop is never blocked.
"""
from __future__ import annotations

import time
from statistics import mean, pstdev
from typing import Optional

import httpx

from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)

_GLASSNODE_URL = "https://api.glassnode.com/v1/metrics/transactions/transfers_volume_to_exchanges_sum"

# asset cache: base -> (ts, list[float] series)
_CACHE: dict[str, tuple[float, list[float]]] = {}


def _base_asset(symbol: str) -> str:
    return symbol.upper().removesuffix("USDT").strip()


async def _fetch_inflow_series(base: str) -> Optional[list[float]]:
    s = get_settings()
    key = s.glassnode_api_key.get_secret_value()
    if not key:
        return None
    now = time.time()
    cached = _CACHE.get(base)
    ttl = float(s.onchain_cache_ttl_seconds)
    if cached and (now - cached[0]) < ttl:
        return cached[1]

    timeout = httpx.Timeout(s.onchain_timeout_seconds)
    params = {"a": base, "i": "24h", "api_key": key}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(_GLASSNODE_URL, params=params)
            r.raise_for_status()
            payload = r.json() or []
    except Exception as exc:  # noqa: BLE001
        log.debug("[ONCHAIN] %s inflow fetch failed: %s", base, exc)
        return None

    series = [float(p["v"]) for p in payload if isinstance(p, dict) and p.get("v") is not None]
    if series:
        _CACHE[base] = (now, series)
    return series or None


async def inflow_spike(symbol: str) -> tuple[bool, str]:
    """Return (is_spike, detail). Bearish when the latest inflow is an outlier.

    A spike is the latest 24h inflow exceeding `mean + z * stdev` of the
    trailing window. Disabled / unavailable → (False, reason).
    """
    s = get_settings()
    if not s.onchain_enabled:
        return False, "onchain_disabled"
    base = _base_asset(symbol)
    series = await _fetch_inflow_series(base)
    if not series or len(series) < 8:
        return False, "onchain_insufficient_data"

    latest = series[-1]
    window = series[:-1][-30:]  # trailing baseline, exclude latest
    if len(window) < 5:
        return False, "onchain_short_window"
    mu = mean(window)
    sigma = pstdev(window)
    if sigma <= 0:
        return False, "onchain_flat_baseline"
    z = (latest - mu) / sigma
    if z >= s.onchain_inflow_spike_z:
        return True, f"inflow z={z:.2f} >= {s.onchain_inflow_spike_z}"
    return False, f"inflow z={z:.2f}"
