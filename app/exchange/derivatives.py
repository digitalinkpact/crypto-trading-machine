"""Derivatives market context: funding rate + open interest.

IMPORTANT: Binance.US is spot-only — it has no perpetual futures, so funding
and open interest don't exist there. When `derivatives_data_enabled=true`, this
module reads PUBLIC market data from Binance global futures (`fapi.binance.com`)
purely as a *reference signal*. It NEVER places orders and holds no credentials.
It may be geofenced from US IPs, so every call is best-effort and non-fatal:
failures return None and the caller simply skips the funding/OI checks.

Signals derived here:
  * funding rate — deeply negative funding means the perp is crowded-short and
    squeeze-prone; we use it to veto new longs.
  * open interest trend — rising OI alongside rising price confirms a trend
    (fresh money), whereas rising price on falling OI is short-covering.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import httpx

from app.config import Settings, get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class DerivContext:
    symbol: str
    funding_rate: Optional[float]      # e.g. 0.0001 = 0.01%
    open_interest: Optional[float]     # base-asset units
    mark_price: Optional[float]


class DerivativesData:
    """Best-effort funding/OI reader for Binance global futures (read-only)."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        # cache: symbol -> (ts, DerivContext)
        self._cache: dict[str, tuple[float, DerivContext]] = {}

    @property
    def enabled(self) -> bool:
        return self._settings.derivatives_data_enabled

    async def context(self, symbol: str) -> Optional[DerivContext]:
        """Funding + OI snapshot for `symbol`, or None when disabled/unavailable."""
        if not self.enabled:
            return None
        sym = symbol.upper()
        now = time.time()
        cached = self._cache.get(sym)
        ttl = float(self._settings.derivatives_cache_ttl_seconds)
        if cached and (now - cached[0]) < ttl:
            return cached[1]

        timeout = httpx.Timeout(self._settings.derivatives_timeout_seconds)
        base = self._settings.derivatives_base_url.rstrip("/")
        funding: Optional[float] = None
        mark: Optional[float] = None
        oi: Optional[float] = None
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                premium = await self._get_json(
                    client, f"{base}/fapi/v1/premiumIndex", {"symbol": sym}
                )
                if isinstance(premium, dict):
                    funding = _to_float(premium.get("lastFundingRate"))
                    mark = _to_float(premium.get("markPrice"))
                oi_payload = await self._get_json(
                    client, f"{base}/fapi/v1/openInterest", {"symbol": sym}
                )
                if isinstance(oi_payload, dict):
                    oi = _to_float(oi_payload.get("openInterest"))
        except Exception as exc:  # noqa: BLE001
            log.debug("[DERIV] %s fetch failed (%s) — skipping", sym, exc)
            return None

        if funding is None and oi is None:
            return None
        ctx = DerivContext(symbol=sym, funding_rate=funding, open_interest=oi, mark_price=mark)
        self._cache[sym] = (now, ctx)
        return ctx

    @staticmethod
    async def _get_json(client: httpx.AsyncClient, url: str, params: dict):
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


def _to_float(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Process-wide singleton.
derivatives = DerivativesData()
