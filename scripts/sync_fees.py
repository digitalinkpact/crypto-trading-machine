"""Sync real Binance.US spot trading fees into .env.

Reads the account's actual maker/taker commission rates from Binance.US and
persists them as BINANCE_MAKER_FEE / BINANCE_TAKER_FEE. The bot then sizes,
fills (paper), labels (ML), and backtests against your true costs instead of
the config defaults.

Run once after adding API keys, or whenever your fee tier changes:

    python -m scripts.sync_fees

Requires BINANCE_API_KEY / BINANCE_API_SECRET to be set (the account endpoint
is signed). Safe to run in paper mode — it only reads fees, never trades.
"""
from __future__ import annotations

import asyncio

from app.config import get_settings
from app.credentials import save_trade_fees
from app.exchange import BinanceUSClient


async def main() -> int:
    s = get_settings()
    if not (
        s.binance_api_key.get_secret_value()
        and s.binance_api_secret.get_secret_value()
    ):
        print(
            "No Binance.US API credentials configured. "
            "Set BINANCE_API_KEY / BINANCE_API_SECRET first, then re-run."
        )
        return 1

    client = BinanceUSClient()
    try:
        fees = await client.trade_fees()
    except Exception as exc:  # noqa: BLE001
        print(f"Failed to fetch trade fees from Binance.US: {exc}")
        return 1

    maker = float(fees["maker"])
    taker = float(fees["taker"])
    save_trade_fees(maker, taker)
    print(f"Saved Binance.US fees: maker={maker:.4%}  taker={taker:.4%}")
    print("Settings cache refreshed — the bot now uses these rates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
