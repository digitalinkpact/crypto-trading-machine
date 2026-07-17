"""Live Binance.US order path diagnostic.

Usage:
    python scripts/test_live_order.py --symbol BTCUSDT --quote-usdt 10

Requires LIVE mode (not paper) and DRY_RUN=false for real execution.
"""
from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal

from app.config import get_settings
from app.exchange import BinanceUSClient, OrderSide, OrderType
from app.exchange.filters import filters
from app.logging_setup import get_logger

log = get_logger(__name__)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--quote-usdt", type=Decimal, default=Decimal("10"))
    args = parser.parse_args()

    s = get_settings()
    print("== Live order diagnostic ==")
    print(f"PAPER_TRADING={s.paper_trading} LIVE_MODE={s.live_mode} DRY_RUN={s.dry_run}")

    if s.paper_trading or s.dry_run:
        raise RuntimeError("This script requires PAPER_TRADING=false and DRY_RUN=false")

    client = BinanceUSClient()

    account = await client.account()
    print(f"Account type: {account.get('accountType')} canTrade={account.get('canTrade')}")
    if not account.get("canTrade", False):
        raise RuntimeError("API key does not have trading permissions")

    balances = {
        b["asset"]: Decimal(str(b["free"]))
        for b in account.get("balances", [])
        if Decimal(str(b.get("free", "0"))) > 0
    }
    print(f"Balances: {balances}")

    await filters.load()
    px = await client.ticker_price(args.symbol)
    raw_qty = args.quote_usdt / px
    qty = filters.round_qty(args.symbol, raw_qty)
    if not filters.meets_min(args.symbol, qty, px):
        raise RuntimeError(f"min notional/qty check failed for {args.symbol}, qty={qty}, px={px}")

    print(f"Placing BUY {args.symbol} qty={qty} (~{args.quote_usdt} USDT)")
    buy = await client.place_order(
        symbol=args.symbol,
        side=OrderSide.BUY,
        type=OrderType.MARKET,
        quantity=qty,
    )
    print(f"BUY status={buy.status} order_id={buy.exchange_order_id} filled={buy.filled_quantity} avg={buy.avg_fill_price}")
    if buy.status.name not in ("FILLED", "PARTIALLY_FILLED"):
        raise RuntimeError(f"BUY failed: {buy.status}")

    post_buy = await client.account()
    base = args.symbol.removesuffix("USDT")
    base_free = next((Decimal(str(b["free"])) for b in post_buy.get("balances", []) if b["asset"] == base), Decimal("0"))
    print(f"Post-BUY base balance {base}: {base_free}")
    if base_free <= 0:
        raise RuntimeError("Position verification failed: no base balance after BUY")

    sell_qty = filters.round_qty(args.symbol, base_free)
    if sell_qty <= 0:
        raise RuntimeError("Rounded SELL qty is zero")
    print(f"Placing SELL {args.symbol} qty={sell_qty}")
    sell = await client.place_order(
        symbol=args.symbol,
        side=OrderSide.SELL,
        type=OrderType.MARKET,
        quantity=sell_qty,
    )
    print(f"SELL status={sell.status} order_id={sell.exchange_order_id} filled={sell.filled_quantity} avg={sell.avg_fill_price}")
    if sell.status.name not in ("FILLED", "PARTIALLY_FILLED"):
        raise RuntimeError(f"SELL failed: {sell.status}")

    print("Live order diagnostic completed successfully")


if __name__ == "__main__":
    asyncio.run(main())
