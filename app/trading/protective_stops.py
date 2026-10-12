"""Exchange-native protective stop orders — a resting STOP_LOSS (market) backstop
that protects an open position even if the bot process dies or the risk loop
stalls. OFF by default (Settings.exchange_native_stops_enabled); the software
risk loop stays PRIMARY. The exchange stop sits exchange_stop_buffer_pct BEYOND
(below, for a long) the software stop, so the software stop fires first in
normal operation and this only catches a bot-down / stalled-loop catastrophe.

All bot stop orders carry a deterministic client-order-id with
exchange_stop_coid_prefix, so orphan cleanup only ever touches our own orders.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from app.config import get_settings
from app.exchange.models import Order, OrderSide
from app.logging_setup import get_logger

log = get_logger(__name__)


def stop_coid(prefix: str, symbol: str, entry_ts: str) -> str:
    """Deterministic client-order-id for a position's protective stop so retries
    never double-place and orphan cleanup can match by prefix. <=36 chars
    (Binance limit)."""
    h = hashlib.sha1(f"{symbol}|{entry_ts}".encode()).hexdigest()[:12]
    return f"{prefix}{symbol}-{h}"[:36]


def backstop_price(software_stop_price: Decimal, buffer_pct: float) -> Decimal:
    """Exchange backstop sits buffer_pct BELOW the software stop (for a long),
    so the software risk loop exits first in normal operation."""
    return Decimal(str(software_stop_price)) * (Decimal("1") - Decimal(str(buffer_pct)))


@dataclass
class StopPlan:
    symbol: str
    qty: Decimal
    stop_price: Decimal
    coid: str


class ProtectiveStopManager:
    """Thin, fully-injectable orchestration over the exchange's stop orders.

    `client` must expose: place_stop_loss, cancel_order, open_orders (the
    BinanceUSClient subset; tests use tests.fakes.FakeExchange).
    """

    def __init__(self, client, *, settings=None) -> None:
        self._client = client
        self._s = settings or get_settings()

    # ── config ───────────────────────────────────────────────────────────
    @property
    def _prefix(self) -> str:
        return getattr(self._s, "exchange_stop_coid_prefix", "pstop-")

    @property
    def _buffer(self) -> float:
        return float(getattr(self._s, "exchange_stop_buffer_pct", 0.01))

    # ── planning / feasibility ───────────────────────────────────────────
    def plan(self, symbol: str, qty, software_stop_price, entry_ts: str) -> StopPlan:
        return StopPlan(
            symbol=symbol,
            qty=Decimal(str(qty)),
            stop_price=backstop_price(software_stop_price, self._buffer),
            coid=stop_coid(self._prefix, symbol, entry_ts),
        )

    @staticmethod
    def feasible(plan: StopPlan, *, min_notional: Decimal) -> tuple[bool, str]:
        """Whether a protective stop CAN be placed — used to BLOCK a new entry
        before buying (never create a position we can't protect)."""
        if plan.qty <= 0:
            return False, "stop_qty_zero"
        if plan.stop_price <= 0:
            return False, "stop_price_invalid"
        if plan.qty * plan.stop_price < min_notional:
            return False, f"stop_below_min_notional({plan.qty * plan.stop_price:.4f}<{min_notional})"
        return True, "ok"

    # ── single-position operations ───────────────────────────────────────
    async def place(self, plan: StopPlan) -> Optional[Order]:
        try:
            order = await self._client.place_stop_loss(
                symbol=plan.symbol, quantity=plan.qty, stop_price=plan.stop_price,
                client_order_id=plan.coid, side=OrderSide.SELL,
            )
            log.info("protective stop PLACED %s qty=%s stop=%s coid=%s",
                     plan.symbol, plan.qty, plan.stop_price, plan.coid)
            return order
        except Exception as exc:  # noqa: BLE001
            log.critical("protective stop PLACE FAILED %s qty=%s stop=%s: %s",
                         plan.symbol, plan.qty, plan.stop_price, exc)
            return None

    async def cancel(self, symbol: str, entry_ts: str) -> str:
        """Cancel a position's resting stop before a software-driven exit.

        Returns 'canceled' (a resting stop was removed — safe to sell) or
        'absent' (nothing to cancel OR it already fired between our check and
        the cancel — the CALLER MUST re-verify free balance before selling to
        avoid a double-sell)."""
        coid = stop_coid(self._prefix, symbol, entry_ts)
        try:
            await self._client.cancel_order(symbol, coid)
            return "canceled"
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "protective stop cancel for %s found nothing to cancel (%s) — stop may "
                "have already fired; caller must re-verify balance before selling",
                symbol, exc,
            )
            return "absent"

    async def replace(self, symbol: str, qty, software_stop_price, entry_ts: str) -> Optional[Order]:
        """Cancel + re-place for the remaining qty after a TP1/TP2 partial exit,
        or at a higher ratcheted stop. Idempotent (same coid)."""
        await self.cancel(symbol, entry_ts)
        return await self.place(self.plan(symbol, qty, software_stop_price, entry_ts))

    async def ratchet(self, symbol: str, qty, software_stop_price, entry_ts: str) -> Optional[Order]:
        """Move the exchange stop UP only (never down). No-op if the resting
        stop is already at/above the new backstop (avoids cancel/replace churn)."""
        new_stop = backstop_price(software_stop_price, self._buffer)
        coid = stop_coid(self._prefix, symbol, entry_ts)
        cur = next((o for o in await self.list_bot_stops(symbol)
                    if o.get("clientOrderId") == coid), None)
        if cur is not None and Decimal(str(cur.get("stopPrice", "0"))) >= new_stop:
            return None
        return await self.replace(symbol, qty, software_stop_price, entry_ts)

    # ── fleet reconciliation ─────────────────────────────────────────────
    async def list_bot_stops(self, symbol: Optional[str] = None) -> list[dict]:
        orders = await self._client.open_orders(symbol)
        return [o for o in orders if str(o.get("clientOrderId", "")).startswith(self._prefix)]

    async def reconcile(self, desired: dict[str, StopPlan]) -> dict:
        """Ensure every open position has its stop and no orphans linger.

        `desired` maps symbol -> StopPlan (one per open position). Places any
        missing stop; cancels ORPHANS = our-prefix stops whose coid matches no
        desired position. NEVER touches non-prefix orders.
        """
        existing = await self.list_bot_stops()
        by_coid = {o.get("clientOrderId"): o for o in existing}
        desired_coids = {p.coid for p in desired.values()}
        placed: list[str] = []
        failed: list[str] = []
        kept: list[str] = []
        canceled_orphans: list[str] = []

        for sym, plan in desired.items():
            if plan.coid in by_coid:
                kept.append(sym)
            elif await self.place(plan) is not None:
                placed.append(sym)
            else:
                failed.append(sym)

        for coid, o in by_coid.items():
            if coid not in desired_coids:
                try:
                    await self._client.cancel_order(o.get("symbol"), coid)
                    canceled_orphans.append(coid)
                except Exception as exc:  # noqa: BLE001
                    log.warning("orphan stop cancel failed %s: %s", coid, exc)

        return {"placed": placed, "failed": failed, "kept": kept,
                "canceled_orphans": canceled_orphans}
