"""Binance.US async client.

Uses `binance-connector` (official). Wraps REST in asyncio.to_thread so the rest
of the app stays async-first. All order placement is gated by `dry_run`.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import pandas as pd
from binance.error import ClientError
from binance.spot import Spot  # type: ignore[import-untyped]

from app.config import Settings, Timeframe, get_settings
from app.logging_setup import get_logger

from .models import Order, OrderSide, OrderStatus, OrderType

log = get_logger(__name__)


def _new_client_order_id(prefix: str = "ctm") -> str:
    """Idempotency key for orders. Binance.US allows up to 36 chars."""
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


def _extract_avg_fill_price(raw: dict[str, Any]) -> Optional[Decimal]:
    """Best-effort average fill price from Binance order payload.

    Priority:
      1) Weighted average from fills[] (price * qty / sum(qty))
      2) cummulativeQuoteQty / executedQty
      3) explicit price field
    """
    fills = raw.get("fills")
    if isinstance(fills, list) and fills:
        total_qty = Decimal("0")
        total_quote = Decimal("0")
        for fill in fills:
            try:
                qty = Decimal(str(fill.get("qty", "0")))
                px = Decimal(str(fill.get("price", "0")))
            except (ValueError, TypeError, ArithmeticError):  # noqa: BLE001
                continue
            if qty <= 0 or px <= 0:
                continue
            total_qty += qty
            total_quote += (qty * px)
        if total_qty > 0:
            return total_quote / total_qty

    try:
        executed_qty = Decimal(str(raw.get("executedQty", "0")))
        cum_quote = Decimal(str(raw.get("cummulativeQuoteQty", "0")))
        if executed_qty > 0 and cum_quote > 0:
            return cum_quote / executed_qty
    except (ValueError, TypeError, ArithmeticError):  # noqa: BLE001
        pass

    try:
        px = Decimal(str(raw.get("price", "0")))
        if px > 0:
            return px
    except (ValueError, TypeError, ArithmeticError):  # noqa: BLE001
        pass
    return None


class BinanceUSClient:
    """Thin async wrapper around binance-connector's Spot client."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._spot = Spot(
            api_key=self._settings.binance_api_key.get_secret_value() or None,
            api_secret=self._settings.binance_api_secret.get_secret_value() or None,
            base_url=self._settings.binance_base_url,
        )

    # ── Market data ──────────────────────────────────────────────────────
    async def klines(
        self,
        symbol: str,
        timeframe: Timeframe | str,
        limit: int = 500,
    ) -> pd.DataFrame:
        """Fetch OHLCV candles. Returns a DataFrame indexed by close_time (UTC)."""
        interval = timeframe.value if hasattr(timeframe, "value") else str(timeframe)
        raw = await asyncio.to_thread(
            self._spot.klines, symbol, interval, limit=limit
        )
        cols = [
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_base_volume", "taker_quote_volume", "ignore",
        ]
        df = pd.DataFrame(raw, columns=cols)
        for c in ("open", "high", "low", "close", "volume", "quote_volume"):
            df[c] = pd.to_numeric(df[c])
        df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
        df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
        return df.set_index("close_time")[
            ["open", "high", "low", "close", "volume", "quote_volume", "trades", "open_time"]
        ]

    async def ticker_price(self, symbol: str) -> Decimal:
        data = await asyncio.to_thread(self._spot.ticker_price, symbol)
        return Decimal(str(data["price"]))

    async def order_book(self, symbol: str, limit: int = 10) -> dict[str, Any]:
        """Fetch the L2 order book (top `limit` levels each side).

        Public endpoint — no auth required. Returns the raw Binance.US payload
        with `bids` and `asks` as lists of [price, qty] string pairs.
        """
        return await asyncio.to_thread(self._spot.depth, symbol, limit=limit)

    async def account(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._spot.account)

    async def trade_fees(self) -> dict[str, Decimal]:
        """Read this account's REAL spot maker/taker fee rates from Binance.US.

        Returns fractions (e.g. ``Decimal("0.001")`` == 0.10%). Prefers the
        ``commissionRates`` block (already decimal strings); falls back to the
        legacy integer ``makerCommission``/``takerCommission`` fields, which are
        expressed in units of 1/10000 (15 -> 0.0015). Signed endpoint — requires
        API credentials. Raises if neither form is present.
        """
        acct = await asyncio.to_thread(self._spot.account)
        rates = acct.get("commissionRates") or {}

        def _rate(decimal_key: str, int_key: str) -> Optional[Decimal]:
            v = rates.get(decimal_key)
            if v is not None:
                return Decimal(str(v))
            iv = acct.get(int_key)
            if iv is not None:
                return Decimal(str(iv)) / Decimal("10000")
            return None

        maker = _rate("maker", "makerCommission")
        taker = _rate("taker", "takerCommission")
        if maker is None or taker is None:
            raise RuntimeError(
                "Binance.US account payload had no commission rates "
                "(commissionRates / makerCommission / takerCommission missing)"
            )
        return {"maker": maker, "taker": taker}

    # ── Orders ───────────────────────────────────────────────────────────
    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        type: OrderType,
        quantity: Decimal,
        price: Optional[Decimal] = None,
        client_order_id: Optional[str] = None,
    ) -> Order:
        """Place an order. Honors `Settings.dry_run` — defaults to safe."""
        coid = client_order_id or _new_client_order_id()
        order = Order(
            symbol=symbol,
            side=side,
            type=type,
            quantity=quantity,
            price=price,
            client_order_id=coid,
            submitted_at=datetime.now(timezone.utc),
        )

        if self._settings.dry_run or self._settings.paper_trading:
            log.warning(
                "[DRY-RUN] %s %s %s qty=%s price=%s coid=%s",
                symbol, side.value, type.value, quantity, price, coid,
            )
            return order.model_copy(update={"status": OrderStatus.DRY_RUN})

        params: dict[str, Any] = {
            "symbol": symbol,
            "side": side.value,
            "type": type.value,
            "quantity": str(quantity),
            "newClientOrderId": coid,
        }
        if type is OrderType.LIMIT:
            if price is None:
                raise ValueError("LIMIT order requires price")
            params["price"] = str(price)
            params["timeInForce"] = "GTC"

        log.info("Submitting order coid=%s symbol=%s side=%s", coid, symbol, side.value)
        raw = await asyncio.to_thread(self._spot.new_order, **params)
        return order.model_copy(
            update={
                "status": OrderStatus(raw.get("status", "NEW")),
                "exchange_order_id": str(raw.get("orderId")),
                "filled_quantity": Decimal(str(raw.get("executedQty", "0"))),
                "avg_fill_price": _extract_avg_fill_price(raw),
                "raw": raw,
            }
        )

    async def cancel_order(self, symbol: str, client_order_id: str) -> dict[str, Any]:
        if self._settings.dry_run or self._settings.paper_trading:
            log.warning("[DRY-RUN] cancel coid=%s symbol=%s", client_order_id, symbol)
            return {"status": "DRY_RUN", "origClientOrderId": client_order_id}
        return await asyncio.to_thread(
            self._spot.cancel_order, symbol=symbol, origClientOrderId=client_order_id
        )

    async def open_orders(self, symbol: Optional[str] = None) -> list[dict[str, Any]]:
        kwargs = {"symbol": symbol} if symbol else {}
        return await asyncio.to_thread(self._spot.get_open_orders, **kwargs)

    @staticmethod
    def generate_client_order_id(prefix: str = "ctm") -> str:
        """Public accessor for the idempotency-key generator, so a caller can
        mint a `client_order_id` *before* calling `place_order` — needed to
        look the order back up if the placement call itself raises (network
        drop, timeout) and the response was never received."""
        return _new_client_order_id(prefix)

    @staticmethod
    def order_from_raw(
        symbol: str,
        side: OrderSide,
        type: OrderType,
        quantity: Decimal,
        client_order_id: str,
        raw: dict[str, Any],
    ) -> Order:
        """Reconstruct an `Order` from a raw Binance order payload — used when
        recovering the true outcome of an order whose placement call raised
        (so we never got `place_order`'s normal return value) via
        `get_order_by_client_id`. Mirrors `place_order`'s own reconstruction
        so downstream fill/recording logic behaves identically either way."""
        order = Order(
            symbol=symbol,
            side=side,
            type=type,
            quantity=quantity,
            client_order_id=client_order_id,
            submitted_at=datetime.now(timezone.utc),
        )
        return order.model_copy(
            update={
                "status": OrderStatus(raw.get("status", "NEW")),
                "exchange_order_id": str(raw.get("orderId")),
                "filled_quantity": Decimal(str(raw.get("executedQty", "0"))),
                "avg_fill_price": _extract_avg_fill_price(raw),
                "raw": raw,
            }
        )

    async def get_order_by_client_id(
        self, symbol: str, client_order_id: str
    ) -> tuple[str, Optional[dict[str, Any]]]:
        """Query Binance for an order by its client_order_id — the
        authoritative way to learn whether an order that raised during
        placement (timeout, dropped connection) actually reached the
        exchange, instead of guessing. Binance dedupes on this idempotency
        key, so if the request DID land despite us losing the response, this
        finds the real order.

        Returns:
          ("found", raw_order)       — Binance has a record; raw_order has
                                        the real status/executedQty/etc.
          ("confirmed_absent", None) — Binance explicitly says no such order
                                        exists (error -2013) — it truly never
                                        arrived; safe to treat as not placed.
          ("inconclusive", None)     — the query itself failed (still can't
                                        reach Binance, auth error, etc). This
                                        proves NOTHING — callers must NOT
                                        treat this as either success or
                                        failure.
        """
        try:
            raw = await asyncio.to_thread(
                self._spot.get_order, symbol=symbol, origClientOrderId=client_order_id
            )
            return "found", raw
        except ClientError as e:
            if e.error_code == -2013:
                return "confirmed_absent", None
            log.warning(
                "get_order_by_client_id inconclusive for symbol=%s coid=%s: %s",
                symbol, client_order_id, e,
            )
            return "inconclusive", None
        except Exception as e:  # noqa: BLE001
            log.warning(
                "get_order_by_client_id inconclusive for symbol=%s coid=%s: %s",
                symbol, client_order_id, e,
            )
            return "inconclusive", None

    # ── Liquidation ──────────────────────────────────────────────────────
    async def liquidate_all(self, quote: str = "USDT") -> list[Order]:
        """Market-sell every non-quote balance into `quote`.

        Used by the Autopilot Stop button. Honors `dry_run` — if dry_run is on,
        this is a no-op that just logs what *would* happen. The user has to
        turn dry_run off explicitly to actually liquidate live.
        """
        account = await asyncio.to_thread(self._spot.account)
        results: list[Order] = []
        for bal in account.get("balances", []):
            asset = bal["asset"]
            free = Decimal(bal["free"])
            if asset == quote or free <= 0:
                continue
            symbol = f"{asset}{quote}"
            coid = _new_client_order_id("liq")
            if self._settings.dry_run:
                log.warning(
                    "[DRY-RUN] liquidate %s qty=%s coid=%s",
                    symbol, free, coid,
                )
                results.append(
                    Order(
                        symbol=symbol,
                        side=OrderSide.SELL,
                        type=OrderType.MARKET,
                        quantity=free,
                        client_order_id=coid,
                        status=OrderStatus.DRY_RUN,
                        submitted_at=datetime.now(timezone.utc),
                    )
                )
                continue
            params = {
                "symbol": symbol,
                "side": OrderSide.SELL.value,
                "type": OrderType.MARKET.value,
                "quantity": str(free),
                "newClientOrderId": coid,
            }
            try:
                raw = await asyncio.to_thread(self._spot.new_order, **params)
            except Exception as exc:  # noqa: BLE001
                log.warning("liquidate %s failed: %s", symbol, exc)
                continue
            results.append(
                Order(
                    symbol=symbol,
                    side=OrderSide.SELL,
                    type=OrderType.MARKET,
                    quantity=free,
                    client_order_id=coid,
                    status=OrderStatus(raw.get("status", "NEW")),
                    exchange_order_id=str(raw.get("orderId")),
                    submitted_at=datetime.now(timezone.utc),
                    filled_quantity=Decimal(str(raw.get("executedQty", "0"))),
                    avg_fill_price=_extract_avg_fill_price(raw),
                    raw=raw,
                )
            )
            log.warning("liquidated %s qty=%s coid=%s", symbol, free, coid)
        return results
