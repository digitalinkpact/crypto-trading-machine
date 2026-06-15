import asyncio
import time
from decimal import Decimal, InvalidOperation
from typing import List, Optional

import httpx

from app.config import Timeframe, get_settings
from app.exchange.client import BinanceUSClient
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
_LIQUID_CACHE: dict = {"symbols": None, "timestamp": 0.0}


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


async def _probe_symbol(
    client: BinanceUSClient,
    symbol: str,
    *,
    depth_limit: int,
    min_days: int,
    sem: asyncio.Semaphore,
) -> tuple[str, Optional[float], Optional[int]]:
    """Probe one symbol's top-of-book spread (%) and available history (days).

    Returns (symbol, spread_pct_or_None, age_days_or_None). Any error yields
    None for that field and the caller FAILS OPEN (keeps the symbol) — the
    execution-time order-book gate remains the hard money-guard. `age_days` is
    the count of daily candles returned (capped at the requested limit), i.e. a
    coin with >= min_days candles is treated as old enough.
    """
    async with sem:
        spread_pct: Optional[float] = None
        age_days: Optional[int] = None
        try:
            book = await client.order_book(symbol, limit=depth_limit)
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if bids and asks:
                bid = Decimal(str(bids[0][0]))
                ask = Decimal(str(asks[0][0]))
                mid = (bid + ask) / Decimal(2)
                if mid > 0:
                    spread_pct = float((ask - bid) / mid) * 100.0
        except (InvalidOperation, ValueError, IndexError, TypeError, KeyError) as exc:
            log.debug("[UNIVERSE] %s depth probe failed: %s", symbol, exc)
        except Exception as exc:  # noqa: BLE001
            log.debug("[UNIVERSE] %s depth probe error: %s", symbol, exc)
        if min_days > 0:
            try:
                df = await client.klines(symbol, Timeframe.D1, limit=min_days)
                age_days = int(len(df))
            except Exception as exc:  # noqa: BLE001
                log.debug("[UNIVERSE] %s age probe failed: %s", symbol, exc)
    return symbol, spread_pct, age_days


async def fetch_liquid_universe(
    client: Optional[BinanceUSClient] = None,
) -> List[str]:
    """Build a high-liquidity tradable universe via a staged filter pipeline.

    Stages: top-N by volume -> 24h volume floor -> listing-age floor -> spread
    cap -> top-N survivors. Cached for `volume_refresh_seconds`. Falls back to
    fetch_dynamic_symbols, then the static list, on any top-level failure.
    """
    s = get_settings()
    refresh_s = int(getattr(s, "volume_refresh_seconds", 1800))
    now = time.time()
    if _LIQUID_CACHE["symbols"] and (now - _LIQUID_CACHE["timestamp"] < refresh_s):
        return _LIQUID_CACHE["symbols"]

    base_url = s.binance_base_url.rstrip("/")
    exclude_leveraged = getattr(s, "exclude_leveraged_tokens", True)
    sort_key = getattr(s, "volume_sort_key", "quoteVolume") or "quoteVolume"
    universe_size = int(getattr(s, "universe_size", 75))
    min_vol = float(getattr(s, "min_24h_volume", 0.0) or 0.0)
    max_spread = float(getattr(s, "max_spread_percent", 100.0))
    min_days = int(getattr(s, "min_days_listed", 0))
    final_size = int(getattr(s, "final_pairlist_size", 50))
    concurrency = int(getattr(s, "liquidity_probe_concurrency", 8))

    try:
        async with httpx.AsyncClient(timeout=20) as http:
            ex = await http.get(f"{base_url}/api/v3/exchangeInfo")
            ex.raise_for_status()
            tk = await http.get(f"{base_url}/api/v3/ticker/24hr")
            tk.raise_for_status()

        tradable = [
            d["symbol"]
            for d in ex.json()["symbols"]
            if d["symbol"].endswith("USDT")
            and d["status"] == "TRADING"
            and not (exclude_leveraged and _is_leveraged(d["symbol"]))
            and not _is_stable_pair(d["symbol"])
        ]
        tradable_set = set(tradable)
        vol: dict[str, float] = {}
        for row in tk.json():
            sym = row.get("symbol")
            if sym in tradable_set:
                try:
                    vol[sym] = float(row.get(sort_key, row.get("quoteVolume", 0.0)) or 0.0)
                except (TypeError, ValueError):
                    vol[sym] = 0.0

        # Stage 1 — top-N candidates by volume.
        ranked = sorted(tradable, key=lambda x: vol.get(x, 0.0), reverse=True)
        candidates = ranked[:universe_size]
        log.info(
            "[UNIVERSE] stage1 top-%d by %s: %d candidates (of %d tradable)",
            universe_size, sort_key, len(candidates), len(tradable),
        )

        # Stage 2 — 24h volume floor (uses already-fetched ticker data).
        after_vol = [c for c in candidates if vol.get(c, 0.0) >= min_vol]
        log.info(
            "[UNIVERSE] stage2 24h vol >= $%.0f: %d passed (%d dropped)",
            min_vol, len(after_vol), len(candidates) - len(after_vol),
        )

        # Stages 3+4 — per-symbol listing age + spread (concurrent probes).
        client = client or BinanceUSClient()
        sem = asyncio.Semaphore(concurrency)
        probes = await asyncio.gather(
            *(
                _probe_symbol(client, c, depth_limit=5, min_days=min_days, sem=sem)
                for c in after_vol
            )
        )

        survivors: list[str] = []
        spread_rejects: list[tuple[str, float]] = []
        age_rejects: list[str] = []
        for sym, spread_pct, age_days in probes:
            # Fail-open: unknown spread/age keeps the symbol.
            if min_days > 0 and age_days is not None and age_days < min_days:
                age_rejects.append(sym)
                continue
            if spread_pct is not None and spread_pct > max_spread:
                spread_rejects.append((sym, spread_pct))
                continue
            survivors.append(sym)

        log.info(
            "[UNIVERSE] stage3 listed >= %dd: %d dropped (%s)",
            min_days, len(age_rejects), ", ".join(age_rejects) or "none",
        )
        log.info(
            "[UNIVERSE] stage4 spread <= %.2f%%: %d dropped for spread (%s)",
            max_spread, len(spread_rejects),
            ", ".join(f"{x}@{p:.2f}%" for x, p in spread_rejects) or "none",
        )

        # Stage 5 — keep the top-N survivors (volume-ranked).
        survivors.sort(key=lambda x: vol.get(x, 0.0), reverse=True)
        final = survivors[:final_size]
        final.sort()  # alphabetical for stable downstream output

        _LIQUID_CACHE["symbols"] = final
        _LIQUID_CACHE["timestamp"] = now
        log.info(
            "[UNIVERSE] final top-%d liquid coins (%d loaded): %s",
            final_size, len(final), final,
        )
        return final
    except Exception as exc:  # noqa: BLE001
        log.warning("[UNIVERSE] liquid pairlist build failed: %s. Falling back.", exc)
        try:
            return await fetch_dynamic_symbols()
        except Exception:  # noqa: BLE001
            return list(getattr(s, "static_symbols", []))

