"""Portfolio valuation — works for both LIVE (Binance.US) and PAPER modes."""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.config import get_settings
from app.exchange import BinanceUSClient
from app.logging_setup import get_logger

log = get_logger(__name__)


async def portfolio_snapshot(
    client: Optional[BinanceUSClient] = None,
    mode: Optional[str] = None,
) -> dict:
    """Return total + per-asset portfolio value in USDT.

    `mode` overrides `settings.paper_trading`. When PAPER, balances come from
    the SQLite store; when LIVE, from Binance.US `account()`.
    """
    if mode is None:
        mode = "paper" if get_settings().paper_trading else "live"

    if mode == "paper":
        # Local import to avoid a circular import at module load time.
        from app.trading.paper import paper_exchange
        return await paper_exchange.snapshot()

    client = client or BinanceUSClient()
    account = await client.account()
    can_trade = bool(account.get("canTrade", True))
    account_type = account.get("accountType", "SPOT")
    raw_balances = [
        (
            b["asset"],
            Decimal(str(b.get("free", "0"))),
            Decimal(str(b.get("locked", "0"))),
        )
        for b in account.get("balances", [])
    ]

    usdt_cash = Decimal("0")
    holdings: list[dict] = []
    all_balances: dict[str, Decimal] = {}
    free_balances: dict[str, Decimal] = {}
    for asset, free_qty, locked_qty in raw_balances:
        qty = free_qty + locked_qty
        if qty <= 0:
            continue
        all_balances[asset] = qty
        if free_qty > 0:
            free_balances[asset] = free_qty
        if asset == "USDT":
            usdt_cash = free_qty
            continue
        symbol = f"{asset}USDT"
        try:
            price = await client.ticker_price(symbol)
        except Exception as exc:  # noqa: BLE001
            log.debug("price fetch failed for %s: %s", symbol, exc)
            continue
        value = qty * price
        holdings.append({
            "asset": asset, "qty": qty,
            "price_usdt": price, "value_usdt": value,
        })

    total = usdt_cash + sum((h["value_usdt"] for h in holdings), Decimal("0"))
    return {
        "total_usdt": total,
        "usdt_cash": usdt_cash,
        "holdings": holdings,
        "all_balances": all_balances,
        "free_balances": free_balances,
        "can_trade": can_trade,
        "account_type": account_type,
    }
