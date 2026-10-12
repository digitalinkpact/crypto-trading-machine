"""Test doubles for the exchange boundary — no network, in-memory state.

FakeExchange mirrors the subset of BinanceUSClient the protective-stop manager
uses (place_stop_loss / cancel_order / open_orders / get_order / account) AND
models LOCKED balances: a resting stop locks the base qty (free -> locked), a
cancel releases it, a fill consumes it. This reproduces the real hazard where
free reads 0 while a stop rests, plus the cancel-then-sell race.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Optional

from app.exchange.models import Order, OrderSide, OrderStatus, OrderType


class FakeOrderNotFound(Exception):
    """cancel_order on an order already gone (filled/absent) — mirrors
    Binance -2011 'Unknown order sent.'"""


def _base(symbol: str) -> str:
    return symbol[:-4] if symbol.endswith("USDT") else symbol


class FakeExchange:
    def __init__(self) -> None:
        self.open: dict[str, dict] = {}        # coid -> raw open order
        self.filled: dict[str, dict] = {}       # coid -> raw filled order
        self.placed: list[dict] = []
        self.canceled: list[str] = []
        self.fail_place: bool = False
        self._next_id = 1000
        self._bal: dict[str, dict] = defaultdict(lambda: {"free": Decimal("0"), "locked": Decimal("0")})
        self.fee_rate = Decimal("0.001")       # fake 0.1% commission on a fill

    # ── balance helpers (test setup + assertions) ────────────────────────
    def seed_balance(self, asset: str, free) -> None:
        self._bal[asset]["free"] = Decimal(str(free))

    def free(self, asset: str) -> Decimal:
        return self._bal[asset]["free"]

    def locked(self, asset: str) -> Decimal:
        return self._bal[asset]["locked"]

    # ── exchange API subset ──────────────────────────────────────────────
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
        base = _base(symbol)
        q = Decimal(str(quantity))
        lock = min(self._bal[base]["free"], q)   # can only lock what's free
        self._bal[base]["free"] -= lock
        self._bal[base]["locked"] += lock
        coid = client_order_id or f"fake-{len(self.placed)}"
        self._next_id += 1
        raw = {
            "symbol": symbol, "clientOrderId": coid, "orderId": self._next_id,
            "type": "STOP_LOSS", "side": side.value, "origQty": str(q),
            "stopPrice": str(stop_price), "status": "NEW", "_locked": str(lock),
        }
        self.open[coid] = raw
        self.placed.append(raw)
        return Order(
            symbol=symbol, side=side, type=OrderType.STOP_LOSS, quantity=q,
            stop_price=Decimal(str(stop_price)), client_order_id=coid,
            status=OrderStatus.NEW, exchange_order_id=str(raw["orderId"]), raw=raw,
        )

    async def cancel_order(self, symbol: str, client_order_id: str) -> dict:
        self.canceled.append(client_order_id)
        raw = self.open.pop(client_order_id, None)
        if raw is None:
            raise FakeOrderNotFound(client_order_id)
        base = _base(symbol)
        lk = Decimal(str(raw.get("_locked", "0")))
        self._bal[base]["locked"] -= lk
        self._bal[base]["free"] += lk            # release the lock back to free
        return {"status": "CANCELED", "origClientOrderId": client_order_id}

    async def open_orders(self, symbol: Optional[str] = None) -> list[dict]:
        return [o for o in self.open.values() if symbol is None or o["symbol"] == symbol]

    async def get_order(self, symbol: str, client_order_id: str) -> Optional[dict]:
        return self.open.get(client_order_id) or self.filled.get(client_order_id)

    async def account(self) -> dict:
        return {"balances": [
            {"asset": a, "free": str(v["free"]), "locked": str(v["locked"])}
            for a, v in self._bal.items() if v["free"] or v["locked"]
        ]}

    # ── test helpers ─────────────────────────────────────────────────────
    def trigger(self, client_order_id: str, fill_price) -> None:
        """Simulate the stop firing: consume the locked qty, record the fill."""
        raw = self.open.pop(client_order_id, None)
        if raw is None:
            return
        base = _base(raw["symbol"])
        lk = Decimal(str(raw.get("_locked", "0")))
        self._bal[base]["locked"] -= lk           # sold on the exchange
        price = Decimal(str(fill_price))
        raw["status"] = "FILLED"
        raw["fillPrice"] = str(price)
        raw["commission"] = str(lk * price * self.fee_rate)
        raw["commissionAsset"] = "USDT"
        self.filled[client_order_id] = raw

    def inject_open(self, symbol: str, client_order_id: str, qty: str = "1", stop: str = "1") -> None:
        """Seed an open order directly (e.g. an orphan) without touching balances."""
        self._next_id += 1
        self.open[client_order_id] = {
            "symbol": symbol, "clientOrderId": client_order_id, "orderId": self._next_id,
            "type": "STOP_LOSS", "side": "SELL", "origQty": qty, "stopPrice": stop,
            "status": "NEW", "_locked": "0",
        }
