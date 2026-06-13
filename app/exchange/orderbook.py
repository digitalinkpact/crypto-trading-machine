"""Order-book analysis and a pre-trade liquidity gate.

Before sending an entry, we inspect the live L2 book so we don't market into a
thin or wide spread and eat avoidable slippage. The gate is intentionally
*fail-open*: if the book can't be fetched (network blip, delisted symbol), the
trade is allowed and the existing technicals-based logic is unaffected.

Pure functions here (`analyze_order_book`, `estimate_slippage`) take already
-parsed bid/ask ladders so they're trivially unit-testable without the network.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from app.config import get_settings
from app.exchange.client import BinanceUSClient
from app.logging_setup import get_logger
from app.signals import SignalAction

log = get_logger(__name__)

# Ladder = list of (price, qty) levels, best price first.
Ladder = list[tuple[Decimal, Decimal]]


def _parse_levels(rows: list) -> Ladder:
    out: Ladder = []
    for row in rows or []:
        try:
            px = Decimal(str(row[0]))
            qty = Decimal(str(row[1]))
        except (InvalidOperation, ValueError, IndexError, TypeError):
            continue
        if px > 0 and qty > 0:
            out.append((px, qty))
    return out


@dataclass(frozen=True)
class BookMetrics:
    """Liquidity snapshot derived from the top of the order book."""

    best_bid: Decimal
    best_ask: Decimal
    mid: Decimal
    spread_pct: float
    # Quote-denominated (USDT) resting depth within `near_pct` of mid.
    bid_depth_quote: Decimal
    ask_depth_quote: Decimal

    def depth_for(self, side: SignalAction) -> Decimal:
        """Depth that a taker on `side` would consume (asks for BUY, bids for SELL)."""
        return self.ask_depth_quote if side == SignalAction.BUY else self.bid_depth_quote


def analyze_order_book(
    bids: Ladder,
    asks: Ladder,
    *,
    near_pct: float = 0.001,
) -> Optional[BookMetrics]:
    """Compute spread and near-mid depth from parsed bid/ask ladders.

    `near_pct` is the band around mid used for the depth sum (0.001 = 0.1%).
    Returns None if either side is empty.
    """
    if not bids or not asks:
        return None
    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / Decimal(2)
    if mid <= 0:
        return None
    spread_pct = float((best_ask - best_bid) / mid)

    band = Decimal(str(near_pct))
    low = mid * (Decimal(1) - band)
    high = mid * (Decimal(1) + band)

    bid_depth = sum((px * qty for px, qty in bids if px >= low), Decimal(0))
    ask_depth = sum((px * qty for px, qty in asks if px <= high), Decimal(0))

    return BookMetrics(
        best_bid=best_bid,
        best_ask=best_ask,
        mid=mid,
        spread_pct=spread_pct,
        bid_depth_quote=bid_depth,
        ask_depth_quote=ask_depth,
    )


def estimate_slippage(
    asks_or_bids: Ladder,
    trade_quote: Decimal,
    *,
    side: SignalAction,
) -> Optional[float]:
    """Estimate fill slippage (fraction) for a market order of `trade_quote` USDT.

    Walks the relevant ladder (asks for a BUY taker, bids for a SELL taker),
    accumulating quote spent until the notional is filled, then compares the
    volume-weighted average fill price to the best price.
    Returns None if the book can't cover the notional.
    """
    if not asks_or_bids or trade_quote <= 0:
        return None
    best = asks_or_bids[0][0]
    remaining = trade_quote
    spent = Decimal(0)
    filled_base = Decimal(0)
    for px, qty in asks_or_bids:
        level_quote = px * qty
        take_quote = min(level_quote, remaining)
        if take_quote <= 0:
            break
        filled_base += take_quote / px
        spent += take_quote
        remaining -= take_quote
        if remaining <= 0:
            break
    if remaining > 0 or filled_base <= 0:
        return None  # book too thin to fully fill
    avg_px = spent / filled_base
    if side == SignalAction.BUY:
        slip = (avg_px - best) / best
    else:
        slip = (best - avg_px) / best
    return float(slip)


async def liquidity_gate(
    symbol: str,
    side: SignalAction,
    trade_quote: Decimal,
    *,
    client: Optional[BinanceUSClient] = None,
) -> tuple[bool, str]:
    """Decide whether `symbol` has a tight enough book for a `trade_quote` entry.

    Returns (ok, detail). FAIL-OPEN: any fetch/parse error returns (True, ...)
    so a transient outage never blocks trading. Also logs estimated slippage.
    """
    s = get_settings()
    if not s.orderbook_gate_enabled:
        return True, "gate_disabled"

    client = client or BinanceUSClient()
    try:
        raw = await client.order_book(symbol, limit=s.orderbook_depth_limit)
    except Exception as exc:  # noqa: BLE001
        log.debug("[OB_GATE] %s book fetch failed (%s) — allowing (fail-open)", symbol, exc)
        return True, f"book_unavailable:{exc}"

    bids = _parse_levels(raw.get("bids", []))
    asks = _parse_levels(raw.get("asks", []))
    metrics = analyze_order_book(bids, asks, near_pct=s.orderbook_near_pct)
    if metrics is None:
        log.debug("[OB_GATE] %s empty book — allowing (fail-open)", symbol)
        return True, "empty_book"

    taker_ladder = asks if side == SignalAction.BUY else bids
    slippage = estimate_slippage(taker_ladder, trade_quote, side=side)
    depth = metrics.depth_for(side)
    required_depth = trade_quote * Decimal(str(s.min_depth_trade_multiple))

    slip_str = f"{slippage:.4%}" if slippage is not None else "n/a"
    log.info(
        "[OB_GATE] %s %s spread=%.4f%% depth=%.0f need=%.0f est_slippage=%s",
        symbol, side.value, metrics.spread_pct * 100, float(depth),
        float(required_depth), slip_str,
    )

    if metrics.spread_pct > s.max_spread_pct:
        return False, (
            f"spread {metrics.spread_pct:.4%} > {s.max_spread_pct:.4%}"
        )
    if s.min_depth_trade_multiple > 0 and depth < required_depth:
        return False, (
            f"depth {float(depth):.0f} < {float(required_depth):.0f} "
            f"({s.min_depth_trade_multiple}x trade)"
        )
    return True, f"ok spread={metrics.spread_pct:.4%} slippage={slip_str}"
