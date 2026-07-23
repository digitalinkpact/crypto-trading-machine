"""Tests for the Order Failure Protocol in Autopilot._submit /
Autopilot._resolve_order_after_exception (app/trading/autopilot.py).

Core rule under test: an exception raised by place_order() must NEVER be
treated as "the order failed" — the outcome must be proven via Binance's
authoritative order-by-client-id lookup before Autopilot does anything else,
and an inconclusive lookup must halt new entries rather than guess.
"""
from __future__ import annotations

from decimal import Decimal

import app.trading.autopilot as autopilot_module
import app.trading.watchdog as watchdog_module
from app.exchange import OrderSide, OrderStatus, OrderType
from app.exchange.client import BinanceUSClient
from app.trading.autopilot import Autopilot


class _FakeClient:
    """Stand-in for BinanceUSClient in _resolve_order_after_exception tests."""

    def __init__(self, outcome: str, raw: dict | None = None):
        self._outcome = outcome
        self._raw = raw

    async def get_order_by_client_id(self, symbol: str, client_order_id: str):
        return self._outcome, self._raw

    # Reuse the real (pure, no-network) reconstruction helper.
    order_from_raw = staticmethod(BinanceUSClient.order_from_raw)


async def test_resolve_order_after_exception_confirmed_absent_returns_none(monkeypatch):
    ap = Autopilot()
    monkeypatch.setattr(autopilot_module, "BinanceUSClient", lambda: _FakeClient("confirmed_absent"))

    order = await ap._resolve_order_after_exception(
        symbol="BTCUSDT", side=OrderSide.BUY, qty=Decimal("1"),
        coid="coid-1", exc=ConnectionError("dropped"),
    )
    assert order is None
    assert "confirmed not placed" in ap.state.last_error


async def test_resolve_order_after_exception_found_reconstructs_filled_order(monkeypatch):
    ap = Autopilot()
    raw = {
        "status": "FILLED", "orderId": "999", "executedQty": "1",
        "cummulativeQuoteQty": "100", "fills": [],
    }
    monkeypatch.setattr(autopilot_module, "BinanceUSClient", lambda: _FakeClient("found", raw))

    order = await ap._resolve_order_after_exception(
        symbol="BTCUSDT", side=OrderSide.BUY, qty=Decimal("1"),
        coid="coid-2", exc=TimeoutError("no response"),
    )
    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert order.filled_quantity == Decimal("1")
    assert order.client_order_id == "coid-2"


async def test_resolve_order_after_exception_found_but_not_filled(monkeypatch):
    ap = Autopilot()
    raw = {"status": "EXPIRED", "orderId": "1000", "executedQty": "0"}
    monkeypatch.setattr(autopilot_module, "BinanceUSClient", lambda: _FakeClient("found", raw))

    order = await ap._resolve_order_after_exception(
        symbol="BTCUSDT", side=OrderSide.SELL, qty=Decimal("1"),
        coid="coid-3", exc=TimeoutError("no response"),
    )
    assert order is not None
    assert order.status == OrderStatus.EXPIRED
    assert order.filled_quantity == Decimal("0")


async def test_resolve_order_after_exception_inconclusive_halts_and_returns_none(monkeypatch):
    ap = Autopilot()
    monkeypatch.setattr(autopilot_module, "BinanceUSClient", lambda: _FakeClient("inconclusive"))

    kv_calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        autopilot_module.storage, "kv_set",
        lambda key, value: kv_calls.append((key, value)), raising=True,
    )

    halt_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        watchdog_module, "trigger_emergency_halt",
        lambda reason, *, level="new_entries_blocked": halt_calls.append((reason, level)),
        raising=True,
    )

    order = await ap._resolve_order_after_exception(
        symbol="BTCUSDT", side=OrderSide.BUY, qty=Decimal("1"),
        coid="coid-4", exc=ConnectionError("still down"),
    )

    assert order is None
    assert kv_calls and kv_calls[0][0] == "order_outcome_unknown"
    assert kv_calls[0][1]["client_order_id"] == "coid-4"
    assert halt_calls and halt_calls[0][1] == "order_outcome_unknown"
    assert "UNKNOWN" in ap.state.last_error


async def test_submit_live_order_exception_never_places_second_order(monkeypatch):
    """When place_order raises but Binance confirms the order landed, _submit
    must use the recovered order (no second place_order call)."""
    ap = Autopilot()
    ap.state.mode = "live"
    start_trades = ap.state.trades_executed

    place_order_calls: list[dict] = []

    class _FakeLiveClient:
        def generate_client_order_id(self) -> str:
            return "precomputed-coid"

        async def place_order(self, **kwargs):
            place_order_calls.append(kwargs)
            raise TimeoutError("connection dropped mid-request")

        async def get_order_by_client_id(self, symbol: str, client_order_id: str):
            raw = {
                "status": "FILLED", "orderId": "42", "executedQty": "1",
                "cummulativeQuoteQty": "100", "fills": [],
            }
            return "found", raw

        order_from_raw = staticmethod(BinanceUSClient.order_from_raw)

    monkeypatch.setattr(autopilot_module, "BinanceUSClient", lambda: _FakeLiveClient())
    monkeypatch.setattr(autopilot_module.storage, "record_order", lambda **kwargs: None, raising=True)
    monkeypatch.setattr(autopilot_module.storage, "open_position", lambda **kwargs: None, raising=True)

    async def _fake_price(_self, _symbol: str) -> Decimal:
        return Decimal("100")

    monkeypatch.setattr(Autopilot, "_price", _fake_price, raising=True)

    order = await ap._submit("BTCUSDT", OrderSide.BUY, Decimal("1"), ["test"])

    assert len(place_order_calls) == 1  # only ever called once — no resubmission
    assert order is not None
    assert order.status == OrderStatus.FILLED
    assert ap.state.trades_executed == start_trades + 1


async def test_submit_live_order_exception_inconclusive_returns_none_and_no_book_write(monkeypatch):
    ap = Autopilot()
    ap.state.mode = "live"

    class _FakeLiveClient:
        def generate_client_order_id(self) -> str:
            return "precomputed-coid-2"

        async def place_order(self, **kwargs):
            raise ConnectionError("network unreachable")

        async def get_order_by_client_id(self, symbol: str, client_order_id: str):
            return "inconclusive", None

    monkeypatch.setattr(autopilot_module, "BinanceUSClient", lambda: _FakeLiveClient())

    record_calls: list = []
    monkeypatch.setattr(
        autopilot_module.storage, "record_order",
        lambda **kwargs: record_calls.append(kwargs), raising=True,
    )
    monkeypatch.setattr(autopilot_module.storage, "kv_set", lambda *a, **k: None, raising=True)
    monkeypatch.setattr(
        watchdog_module, "trigger_emergency_halt",
        lambda reason, *, level="new_entries_blocked": None,
        raising=True,
    )

    order = await ap._submit("BTCUSDT", OrderSide.BUY, Decimal("1"), ["test"])

    assert order is None
    assert record_calls == []  # never recorded — outcome unproven, no phantom position
