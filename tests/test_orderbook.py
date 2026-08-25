"""Tests for the pre-trade order-book liquidity analysis (pure functions)."""
from __future__ import annotations

from decimal import Decimal

from app.exchange.orderbook import analyze_order_book, estimate_slippage, liquidity_gate
from app.signals import SignalAction


def _ladder(levels):
    return [(Decimal(str(p)), Decimal(str(q))) for p, q in levels]


def test_analyze_tight_book_spread_and_depth():
    bids = _ladder([(100.0, 5), (99.95, 10)])
    asks = _ladder([(100.05, 5), (100.10, 10)])
    m = analyze_order_book(bids, asks, near_pct=0.001)
    assert m is not None
    # mid = 100.025, spread = (100.05 - 100.0) / 100.025
    assert abs(m.spread_pct - (0.05 / 100.025)) < 1e-9
    # ±0.1% band around mid (99.925 .. 100.125) includes both levels per side.
    assert m.ask_depth_quote == Decimal("100.05") * Decimal("5") + Decimal("100.10") * Decimal("10")
    assert m.bid_depth_quote == Decimal("100") * Decimal("5") + Decimal("99.95") * Decimal("10")


def test_analyze_empty_side_returns_none():
    assert analyze_order_book([], _ladder([(1, 1)])) is None
    assert analyze_order_book(_ladder([(1, 1)]), []) is None


def test_depth_for_side():
    bids = _ladder([(100.0, 5)])
    asks = _ladder([(100.05, 7)])
    m = analyze_order_book(bids, asks, near_pct=0.01)
    assert m.depth_for(SignalAction.BUY) == m.ask_depth_quote
    assert m.depth_for(SignalAction.SELL) == m.bid_depth_quote


def test_estimate_slippage_walks_ladder():
    # Buying 600 USDT: 500 fills at 100, 100 fills at 101.
    asks = _ladder([(100.0, 5), (101.0, 10)])
    slip = estimate_slippage(asks, Decimal("600"), side=SignalAction.BUY)
    assert slip is not None
    # avg fill ≈ 100.166..., best=100 → ~0.00166 slippage
    assert 0.0015 < slip < 0.0018


def test_estimate_slippage_returns_none_when_book_too_thin():
    asks = _ladder([(100.0, 1)])  # only 100 USDT available
    assert estimate_slippage(asks, Decimal("500"), side=SignalAction.BUY) is None


def test_estimate_slippage_sell_side():
    bids = _ladder([(100.0, 5), (99.0, 10)])
    slip = estimate_slippage(bids, Decimal("600"), side=SignalAction.SELL)
    assert slip is not None and slip > 0


class _FakeClient:
    def __init__(self, *, raises: bool = False, book: dict | None = None) -> None:
        self._raises = raises
        self._book = book if book is not None else {"bids": [], "asks": []}

    async def order_book(self, symbol, limit=10):
        if self._raises:
            raise RuntimeError("network blip")
        return self._book


async def test_liquidity_gate_fails_closed_on_fetch_error():
    """A book fetch error must REJECT the entry, not silently allow it — an
    entry must never be sized against liquidity the bot couldn't verify."""
    ok, why = await liquidity_gate(
        "BTCUSDT", SignalAction.BUY, Decimal("50"), client=_FakeClient(raises=True),
    )
    assert ok is False
    assert "book_unavailable" in why


async def test_liquidity_gate_fails_closed_on_empty_book():
    ok, why = await liquidity_gate(
        "BTCUSDT", SignalAction.BUY, Decimal("50"), client=_FakeClient(),
    )
    assert ok is False
    assert why == "empty_book"


async def test_liquidity_gate_disabled_still_allows(monkeypatch):
    import app.exchange.orderbook as ob_module

    class _S:
        orderbook_gate_enabled = False

    monkeypatch.setattr(ob_module, "get_settings", lambda: _S())
    ok, why = await liquidity_gate(
        "BTCUSDT", SignalAction.BUY, Decimal("50"), client=_FakeClient(raises=True),
    )
    assert ok is True
    assert why == "gate_disabled"
