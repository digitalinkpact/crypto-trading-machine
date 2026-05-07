"""Portfolio valuation helpers — sums Binance.US balances priced in USDT."""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.exchange import BinanceUSClient
from app.logging_setup import get_logger

log = get_logger(__name__)


async def portfolio_snapshot(
    client: Optional[BinanceUSClient] = None,
) -> dict:
    """Return current balances and total portfolio value in USDT.

    Returns a dict with keys: total_usdt, usdt_cash, holdings (list of
    {asset, free, price_usdt, value_usdt}). Raises on API failure.
    """
    client = client or BinanceUSClient()
    account = await client.account()
    balances = [
        (b["asset"], Decimal(b["free"]) + Decimal(b.get("locked", "0")))
        for b in account.get("balances", [])
    ]

    usdt_cash = Decimal("0")
    holdings: list[dict] = []
    for asset, qty in balances:
        if qty <= 0:
            continue
        if asset == "USDT":
            usdt_cash = qty
            continue
        symbol = f"{asset}USDT"
        try:
            price = await client.ticker_price(symbol)
        except Exception as exc:  # noqa: BLE001
            log.debug("price fetch failed for %s: %s", symbol, exc)
            continue
        value = qty * price
        holdings.append({
            "asset": asset,
            "qty": qty,
            "price_usdt": price,
            "value_usdt": value,
        })

    total = usdt_cash + sum((h["value_usdt"] for h in holdings), Decimal("0"))
    return {
        "total_usdt": total,
        "usdt_cash": usdt_cash,
        "holdings": holdings,
    }
