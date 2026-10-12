"""Test doubles for the exchange boundary — no network, in-memory state.

FakeExchange mirrors the subset of BinanceUSClient the protective-stop manager
uses (place_stop_loss / cancel_order / open_orders) and can simulate the
cancel-then-sell race (a stop that fills just before we cancel it).
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.exchange.models import Order, OrderSide, OrderStatus, OrderType


class FakeOrderNotFound(Exception):
    """Raised by cancel_order when the order is already gone (filled/absent) —
    mirrors Binance's -2011 'Unknown order sent.'"""


class FakeExchange:
    def __init__(self) -> None:
        self.open: dict[str, dict] = {}        # coid -> raw order
        self.placed: list[dict] = []
        self.canceled: list[str] = []
        self.fail_place: bool = False          # force place_stop_loss to raise
        self.filled: dict[str, dict] = {}       # coid -> raw (simulated triggered)
        self._next_id = 1000

    async def place_stop_loss(
        self,
        symbol: str,
        quantity: Decimal,
        stop_price: Decimal,
        client_order_id: Optional[str] = None,
        side: OrderSide = OrderSide.SELL,
    ) -> Order:
        if self.fail_place:
            raise RuntimeError("simulated exchange place failure")
        coid = client_order_id or f"fake-{len(self.placed)}"
        self._next_id += 1
        raw = {
            "symbol": symbol, "clientOrderId": coid, "orderId": self._next_id,
            "type": "STOP_LOSS", "side": side.value, "origQty": str(quantity),
            "stopPrice": str(stop_price), "status": "NEW",
        }
        self.open[coid] = raw
        self.placed.append(raw)
        return Order(
            symbol=symbol, side=side, type=OrderType.STOP_LOSS, quantity=quantity,
            stop_price=stop_price, client_order_id=coid, status=OrderStatus.NEW,
            exchange_order_id=str(raw["orderId"]), raw=raw,
        )

    async def cancel_order(self, symbol: str, client_order_id: str) -> dict:
        self.canceled.append(client_order_id)
        if client_order_id in self.open:
            raw = self.open.pop(client_order_id)
            return {"status": "CANCELED", "origClientOrderId": client_order_id, "raw": raw}
        # Already gone — filled between our check and cancel, or never existed.
        raise FakeOrderNotFound(client_order_id)

    async def open_orders(self, symbol: Optional[str] = None) -> list[dict]:
        return [o for o in self.open.values() if symbol is None or o["symbol"] == symbol]

    # ── test helpers ─────────────────────────────────────────────────────
    def trigger(self, client_order_id: str, fill_price: Decimal) -> None:
        """Simulate the stop triggering and filling on the exchange."""
        raw = self.open.pop(client_order_id, None)
        if raw is not None:
            raw["status"] = "FILLED"
            raw["fillPrice"] = str(fill_price)
            self.filled[client_order_id] = raw

    def inject_open(self, symbol: str, client_order_id: str, qty: str = "1", stop: str = "1") -> None:
        """Seed an open order directly (e.g. to simulate an orphan)."""
        self._next_id += 1
        self.open[client_order_id] = {
            "symbol": symbol, "clientOrderId": client_order_id, "orderId": self._next_id,
            "type": "STOP_LOSS", "side": "SELL", "origQty": qty, "stopPrice": stop, "status": "NEW",
        }
