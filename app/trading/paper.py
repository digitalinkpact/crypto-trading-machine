"""Paper exchange — simulates fills against live Binance.US ticker prices.

Balances live in SQLite (`paper_balances`) so they survive restarts.
Every fill is also recorded in the shared `orders` and `positions` tables,
which means the per-agent learning history carries straight into LIVE mode.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from app.config import get_settings
from app.exchange import BinanceUSClient, Order, OrderSide, OrderStatus, OrderType
from app.logging_setup import get_logger
from app.storage import storage

log = get_logger(__name__)

# Paper market orders pay the Binance.US taker fee (configured in app.config).
# Kept as a module fallback for callers that import it directly.
PAPER_FEE_RATE = Decimal(str(get_settings().binance_taker_fee))
DEFAULT_PAPER_USDT = Decimal("10000")


class PaperExchange:
    """Drop-in for `BinanceUSClient.place_order` in paper mode."""

    def __init__(self, live_client: Optional[BinanceUSClient] = None) -> None:
        # Used purely for read-only price/exchange_info calls. Public endpoints,
        # no API key required.
        self._live = live_client or BinanceUSClient()

    # ── balances / portfolio ────────────────────────────────────────
    def ensure_seeded(self, amount: Decimal = DEFAULT_PAPER_USDT) -> None:
        if not storage.paper_balances():
            storage.paper_reset(starting_usdt=float(amount))
            log.warning("Paper account seeded with %s USDT", amount)

    async def ticker_price(self, symbol: str) -> Decimal:
        return await self._live.ticker_price(symbol)

    async def snapshot(self) -> dict:
        """Mirror the shape of `portfolio.portfolio_snapshot` for the dashboard."""
        balances = storage.paper_balances()
        usdt_cash = Decimal(str(balances.get("USDT", 0.0)))
        holdings: list[dict] = []
        all_balances = {a: Decimal(str(q)) for a, q in balances.items()}
        for asset, qty in balances.items():
            if asset == "USDT" or qty <= 0:
                continue
            symbol = f"{asset}USDT"
            try:
                price = await self._live.ticker_price(symbol)
            except Exception as exc:  # noqa: BLE001
                log.debug("paper price fetch failed for %s: %s", symbol, exc)
                continue
            value = Decimal(str(qty)) * price
            holdings.append({
                "asset": asset,
                "qty": Decimal(str(qty)),
                "price_usdt": price,
                "value_usdt": value,
            })
        total = usdt_cash + sum((h["value_usdt"] for h in holdings), Decimal("0"))
        return {
            "total_usdt": total,
            "usdt_cash": usdt_cash,
            "holdings": holdings,
            "all_balances": all_balances,
            "can_trade": True,
            "account_type": "PAPER",
        }

    # ── orders ──────────────────────────────────────────────────────
    async def place_order(
        self,
        *,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        agents: Optional[list[str]] = None,
        client_order_id: Optional[str] = None,
    ) -> Order:
        """Simulate a market order using the current live ticker price."""
        price = await self._live.ticker_price(symbol)
        notional = quantity * price
        fee_rate = Decimal(str(get_settings().binance_taker_fee))
        fee = notional * fee_rate
        base = symbol.removesuffix("USDT")

        if side is OrderSide.BUY:
            usdt_needed = notional + fee
            usdt_have = Decimal(str(storage.paper_balance_get("USDT")))
            if usdt_have < usdt_needed:
                raise RuntimeError(
                    f"insufficient paper USDT: need {usdt_needed:.2f}, have {usdt_have:.2f}"
                )
            storage.paper_balance_add("USDT", -usdt_needed)
            storage.paper_balance_add(base, quantity)
            storage.open_position(
                symbol=symbol, mode="paper", qty=quantity,
                entry_price=price, agents=agents or [],
            )
        else:  # SELL — close any existing position
            base_have = Decimal(str(storage.paper_balance_get(base)))
            qty = min(quantity, base_have)
            if qty <= 0:
                raise RuntimeError(f"no {base} to sell in paper account")
            proceeds = qty * price - (qty * price * fee_rate)
            storage.paper_balance_add(base, -qty)
            storage.paper_balance_add("USDT", proceeds)
            storage.close_position(symbol=symbol, exit_price=price)

        storage.record_order(
            mode="paper", symbol=symbol, side=side.value,
            qty=quantity, price=price, fee=fee,
            client_order_id=client_order_id, agents=agents or [],
        )
        return Order(
            symbol=symbol,
            side=side,
            type=OrderType.MARKET,
            quantity=quantity,
            price=price,
            client_order_id=client_order_id or f"paper-{datetime.now().timestamp()}",
            status=OrderStatus.FILLED,
            submitted_at=datetime.now(timezone.utc),
            filled_quantity=quantity,
            avg_fill_price=price,
            raw={"mode": "paper", "fee": str(fee)},
        )

    async def liquidate_all(self) -> None:
        """Sell every non-USDT paper holding back to USDT."""
        balances = storage.paper_balances()
        for asset, qty in list(balances.items()):
            if asset == "USDT" or qty <= 0:
                continue
            symbol = f"{asset}USDT"
            try:
                await self.place_order(
                    symbol=symbol, side=OrderSide.SELL,
                    quantity=Decimal(str(qty)), agents=["liquidate"],
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("paper liquidate %s failed: %s", symbol, exc)


paper_exchange = PaperExchange()
