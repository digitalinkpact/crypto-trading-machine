"""OHLCV repository with a simple pickle-on-disk cache.

Pickle is used instead of parquet so we don't pull in `pyarrow` (heavy and
sometimes unavailable on slim images). Swap to parquet later if you need
cross-language reads of the cache.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd

from app.config import Timeframe, get_settings
from app.exchange import BinanceUSClient
from app.logging_setup import get_logger

log = get_logger(__name__)


class OHLCVRepository:
    """Fetch candles via the exchange wrapper, cache to pickle on disk."""

    def __init__(
        self,
        client: Optional[BinanceUSClient] = None,
        cache_dir: Optional[Path] = None,
    ) -> None:
        self._client = client or BinanceUSClient()
        self._cache_dir = cache_dir or get_settings().data_cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol: str, timeframe: Timeframe) -> Path:
        return self._cache_dir / f"{symbol}_{timeframe.value}.pkl"

    async def get(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int = 500,
        refresh: bool = True,
    ) -> pd.DataFrame:
        path = self._path(symbol, timeframe)
        if not refresh and path.exists():
            return pd.read_pickle(path)
        df = await self._client.klines(symbol, timeframe, limit=limit)
        df.to_pickle(path)
        return df
