"""One-off helper: prints which symbols in app.config.SYMBOLS are actually
tradeable on Binance.US right now.

Usage:
    python -m scripts.check_symbols
"""
from __future__ import annotations

import asyncio

from binance.spot import Spot  # type: ignore[import-untyped]

from app.config import SYMBOLS, get_settings


async def main() -> None:
    spot = Spot(base_url=get_settings().binance_base_url)
    info = await asyncio.to_thread(spot.exchange_info)
    listed = {s["symbol"] for s in info["symbols"] if s.get("status") == "TRADING"}
    valid, invalid = [], []
    for sym in SYMBOLS:
        (valid if sym in listed else invalid).append(sym)
    print(f"valid   ({len(valid)}): {valid}")
    print(f"invalid ({len(invalid)}): {invalid}")


if __name__ == "__main__":
    asyncio.run(main())
