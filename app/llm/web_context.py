"""Optional web context for LLM prompts.

Fetches a tiny market/news snapshot from public endpoints. Designed to be
best-effort and non-fatal: all failures return an empty context.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any


import httpx
from decimal import Decimal
from app.exchange.client import BinanceUSClient

from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)

_CACHE: dict[str, tuple[float, str]] = {}
_LOCK = asyncio.Lock()


def _base_asset(symbol: str) -> str:
    return symbol.removesuffix("USDT").strip().upper()


# Fetch price from Binance.US using our exchange client
async def _binanceus_snapshot(base: str) -> str:
    symbol = f"{base}USDT"
    try:
        client = BinanceUSClient()
        price = await client.ticker_price(symbol)
        return f"binanceus: price_usd={price}"
    except Exception as exc:
        log.debug("binanceus web context failed for %s: %s", base, exc)
        return ""


def _pick_coin_id(search_payload: dict[str, Any], base: str) -> str | None:
    coins = search_payload.get("coins") or []
    if not coins:
        return None
    base_l = base.lower()
    for c in coins:
        if str(c.get("symbol", "")).lower() == base_l:
            return str(c.get("id", "")) or None
    cid = str(coins[0].get("id", ""))
    return cid or None





async def _duckduckgo_snapshot(client: httpx.AsyncClient, base: str) -> str:
    try:
        r = await client.get(
            "https://api.duckduckgo.com/",
            params={
                "q": f"{base} crypto latest news",
                "format": "json",
                "no_html": 1,
                "no_redirect": 1,
            },
        )
        r.raise_for_status()
        payload = r.json() or {}
        lines: list[str] = []
        abstract = str(payload.get("AbstractText") or "").strip()
        if abstract:
            lines.append(abstract)
        for item in (payload.get("RelatedTopics") or [])[:2]:
            txt = str(item.get("Text") or "").strip() if isinstance(item, dict) else ""
            if txt:
                lines.append(txt)
        if not lines:
            return ""
        joined = " | ".join(lines)
        return f"duckduckgo: {joined[:280]}"
    except Exception as exc:  # noqa: BLE001
        log.debug("duckduckgo web context failed for %s: %s", base, exc)
        return ""


async def get_symbol_web_context(symbol: str) -> str:
    """Return a compact web context block for a symbol, or empty string.

    Context is cached by base asset (e.g., BTC) to avoid repeated external
    calls inside a single LLM pass.
    """
    s = get_settings()
    if not s.llm_web_enabled:
        return ""

    base = _base_asset(symbol)
    now = time.time()
    ttl = float(s.llm_web_cache_ttl_seconds)
    cached = _CACHE.get(base)
    if cached and (now - cached[0]) < ttl:
        return cached[1]

    async with _LOCK:
        cached = _CACHE.get(base)
        if cached and (time.time() - cached[0]) < ttl:
            return cached[1]

        timeout = httpx.Timeout(s.llm_web_timeout_seconds)
        headers = {"User-Agent": "crypto-trading-machine/1.0"}
        try:
            async with httpx.AsyncClient(timeout=timeout, headers=headers) as client:
                # Only DuckDuckGo needs httpx now
                binanceus, ddg = await asyncio.gather(
                    _binanceus_snapshot(base),
                    _duckduckgo_snapshot(client, base),
                )
        except Exception as exc:  # noqa: BLE001
            log.debug("web context fetch failed for %s: %s", base, exc)
            return ""

        parts = [p for p in (binanceus, ddg) if p]
        text = "\n".join(parts)
        _CACHE[base] = (time.time(), text)
        return text
