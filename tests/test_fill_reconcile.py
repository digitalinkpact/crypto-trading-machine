"""Exchange fill reconciliation (app/trading/fill_reconcile.py) and partial-fill
booking. All exchange responses are mocked; the fake client has no order
placement or cancellation methods, so any write attempt would raise."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import app.trading.autopilot as autopilot_module
import app.trading.watchdog as watchdog_module
from app.config import Settings
from app.exchange import Order, OrderSide, OrderStatus, OrderType
from app.trading import fill_reconcile
from app.trading.autopilot import Autopilot
from app.trading.fill_reconcile import reconcile_fills, summarize_trades


class ReadOnlyExchange:
    """Mock Binance.US exposing only the two read endpoints reconciliation uses."""

    def __init__(self, orders=None, trades=None):
        self.orders = orders or {}   # coid -> (outcome, raw)
        self.trades = trades or {}   # orderId -> list | Exception
        self.calls: list[tuple] = []

    async def get_order_by_client_id(self, symbol, client_order_id):
        self.calls.append(("get_order", symbol, client_order_id))
        return self.orders.get(client_order_id, ("inconclusive", None))

    async def my_trades(self, symbol, order_id):
        self.calls.append(("my_trades", symbol, order_id))
        result = self.trades.get(order_id, [])
        if isinstance(result, Exception):
            raise result
        return result


def _trade(trade_id, order_id, qty, price="100", commission="0.01", asset="USDT"):
    return {"id": trade_id, "orderId": order_id, "qty": qty, "price": price,
            "quoteQty": str(Decimal(qty) * Decimal(price)), "commission": commission,
            "commissionAsset": asset}


@pytest.fixture
def halts(monkeypatch):
    calls = []
    monkeypatch.setattr(watchdog_module, "trigger_emergency_halt",
                        lambda reason, *, level="new_entries_blocked": calls.append((reason, level)))
    return calls


def _intent(coid, *, symbol="BTCUSDT", side="BUY", qty="1", status=None, age_minutes=10):
    storage = fill_reconcile.storage
    storage.record_order_intent({"client_order_id": coid, "mode": "live", "symbol": symbol,
                                 "side": side, "qty": qty})
    if status:
        storage.update_order_intent(coid, status, {})
    ts = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).isoformat()
    with storage._lock, storage._conn() as connection:
        connection.execute("UPDATE order_intents SET updated_at=? WHERE client_order_id=?", (ts, coid))


def _book_fill(coid, qty, *, side="BUY", status="FILLED"):
    fill_reconcile.storage.record_live_fill(
        symbol="BTCUSDT", side=side, qty=Decimal(qty), position_qty=Decimal(qty), price=Decimal("100"),
        client_order_id=coid, agents=["profitstream_strategy"], status=status, response={},
    )


def _pending():
    return {i["client_order_id"] for i in fill_reconcile.storage.pending_order_intents("live")}


async def test_filled_order_verified_from_trades_with_duplicate_records(halts):
    _intent("ctm-1")
    _book_fill("ctm-1", "2")
    exchange = ReadOnlyExchange(
        orders={"ctm-1": ("found", {"status": "FILLED", "orderId": 11, "executedQty": "2"})},
        trades={11: [_trade(1, 11, "1.5"), _trade(1, 11, "1.5"), _trade(2, 11, "0.5"),
                     _trade(9, 99, "7")]},
    )

    summary = await reconcile_fills("live", client=exchange)

    row = fill_reconcile.storage.fill_reconciliation("ctm-1")
    assert row["stage"] == "FILLED_VERIFIED"
    assert Decimal(row["trade_qty"]) == Decimal("2") and row["trade_count"] == 2
    assert row["exchange_order_id"] == "11" and row["discrepancy"] is None
    assert summary["discrepancies"] == [] and halts == []
    assert "ctm-1" not in _pending()


async def test_api_success_without_trade_records_is_not_a_verified_fill(halts):
    _intent("ctm-2")
    _book_fill("ctm-2", "1")
    exchange = ReadOnlyExchange(
        orders={"ctm-2": ("found", {"status": "FILLED", "orderId": 12, "executedQty": "1"})},
        trades={12: []},
    )

    await reconcile_fills("live", client=exchange)

    assert fill_reconcile.storage.fill_reconciliation("ctm-2")["stage"] == "FILLS_PENDING"


async def test_network_failures_leave_order_unverified_and_intent_pending(halts):
    _intent("ctm-3")
    _intent("ctm-4", status="UNKNOWN")
    exchange = ReadOnlyExchange(
        orders={"ctm-4": ("found", {"status": "FILLED", "orderId": 14, "executedQty": "1"})},
        trades={14: ConnectionError("timeout")},
    )

    summary = await reconcile_fills("live", client=exchange)

    assert fill_reconcile.storage.fill_reconciliation("ctm-3")["stage"] == "UNVERIFIED"
    assert fill_reconcile.storage.fill_reconciliation("ctm-4")["stage"] == "UNVERIFIED"
    assert {"ctm-3", "ctm-4"} <= _pending()
    assert summary["discrepancies"] == [] and halts == []


async def test_partial_fill_then_expired_is_verified_and_resolves_intent(halts):
    _intent("ctm-5", status="PARTIALLY_FILLED")
    _book_fill("ctm-5", "0.4", status="PARTIALLY_FILLED")
    assert "ctm-5" in _pending()
    exchange = ReadOnlyExchange(
        orders={"ctm-5": ("found", {"status": "EXPIRED", "orderId": 15, "executedQty": "0.4"})},
        trades={15: [_trade(1, 15, "0.25"), _trade(2, 15, "0.15")]},
    )

    await reconcile_fills("live", client=exchange)

    assert fill_reconcile.storage.fill_reconciliation("ctm-5")["stage"] == "PARTIAL_TERMINAL_VERIFIED"
    assert "ctm-5" not in _pending()
    assert halts == []


async def test_order_still_working_on_exchange_stays_pending(halts):
    _intent("ctm-6", status="NEW")
    _intent("ctm-7", status="PARTIALLY_FILLED")
    _book_fill("ctm-7", "0.3", status="PARTIALLY_FILLED")
    exchange = ReadOnlyExchange(orders={
        "ctm-6": ("found", {"status": "NEW", "orderId": 16, "executedQty": "0"}),
        "ctm-7": ("found", {"status": "PARTIALLY_FILLED", "orderId": 17, "executedQty": "0.3"}),
    }, trades={17: [_trade(1, 17, "0.3")]})

    await reconcile_fills("live", client=exchange)

    assert fill_reconcile.storage.fill_reconciliation("ctm-6")["stage"] == "ACCEPTED"
    partial = fill_reconcile.storage.fill_reconciliation("ctm-7")
    assert partial["stage"] == "PARTIALLY_FILLED" and not partial["discrepancy"]
    assert {"ctm-6", "ctm-7"} <= _pending()
    assert not halts


async def test_open_partial_fill_missing_from_book_blocks_new_entries(halts):
    _intent("ctm-7b", status="PARTIALLY_FILLED")
    exchange = ReadOnlyExchange(
        orders={"ctm-7b": ("found", {"status": "PARTIALLY_FILLED", "orderId": 27, "executedQty": "0.6"})},
        trades={27: [_trade(1, 27, "0.6")]},
    )

    await reconcile_fills("live", client=exchange)

    row = fill_reconcile.storage.fill_reconciliation("ctm-7b")
    assert row["stage"] == "PARTIALLY_FILLED"
    assert row["discrepancy"] == "exchange_fill_not_recorded"
    assert halts and "ctm-7b" in _pending()


async def test_rejected_and_cancelled_orders_are_terminal_without_fill(halts):
    _intent("ctm-8")
    _intent("ctm-9", side="SELL")
    exchange = ReadOnlyExchange(orders={
        "ctm-8": ("found", {"status": "REJECTED", "orderId": 18, "executedQty": "0"}),
        "ctm-9": ("found", {"status": "CANCELED", "orderId": 19, "executedQty": "0"}),
    })

    await reconcile_fills("live", client=exchange)

    assert fill_reconcile.storage.fill_reconciliation("ctm-8")["stage"] == "EXCHANGE_REJECTED"
    assert fill_reconcile.storage.fill_reconciliation("ctm-9")["stage"] == "CANCELED"
    assert not ({"ctm-8", "ctm-9"} & _pending())
    assert all(call[0] != "my_trades" for call in exchange.calls)


@pytest.mark.parametrize("case", ["unrecorded_fill", "qty_mismatch", "local_without_execution"])
async def test_discrepancies_block_new_entries(halts, case):
    _intent("ctm-10", status="UNKNOWN")
    raw = {"status": "FILLED", "orderId": 20, "executedQty": "1"}
    trades = {20: [_trade(1, 20, "1")]}
    if case == "qty_mismatch":
        _book_fill("ctm-10", "0.5")
    elif case == "local_without_execution":
        _book_fill("ctm-10", "1")
        raw = {"status": "EXPIRED", "orderId": 20, "executedQty": "0"}
    exchange = ReadOnlyExchange(orders={"ctm-10": ("found", raw)}, trades=trades)

    summary = await reconcile_fills("live", client=exchange)

    row = fill_reconcile.storage.fill_reconciliation("ctm-10")
    assert row["discrepancy"]
    assert summary["discrepancies"][0]["client_order_id"] == "ctm-10"
    assert halts and halts[0][1] == "new_entries_blocked"
    if case == "unrecorded_fill":
        # Coins moved on the exchange but not in the book: never auto-resolve.
        assert "ctm-10" in _pending()


async def test_absent_order_respects_grace_window(halts):
    _intent("ctm-11", age_minutes=1)
    _intent("ctm-12", age_minutes=30)
    _intent("ctm-12b", age_minutes=60 * 24 * 45)  # beyond order-history retention margin
    exchange = ReadOnlyExchange(orders={
        "ctm-11": ("confirmed_absent", None), "ctm-12": ("confirmed_absent", None),
        "ctm-12b": ("confirmed_absent", None),
    })

    await reconcile_fills("live", client=exchange)

    assert fill_reconcile.storage.fill_reconciliation("ctm-11")["stage"] == "UNVERIFIED"
    assert fill_reconcile.storage.fill_reconciliation("ctm-12")["stage"] == "NOT_ON_EXCHANGE"
    assert fill_reconcile.storage.fill_reconciliation("ctm-12b")["stage"] == "UNVERIFIED"
    assert {"ctm-11", "ctm-12b"} <= _pending() and "ctm-12" not in _pending()


async def test_reconciliation_is_idempotent_and_skips_verified_orders(halts):
    _intent("ctm-13")
    _book_fill("ctm-13", "1")
    exchange = ReadOnlyExchange(
        orders={"ctm-13": ("found", {"status": "FILLED", "orderId": 23, "executedQty": "1"})},
        trades={23: [_trade(1, 23, "1")]},
    )

    await reconcile_fills("live", client=exchange)
    second = await reconcile_fills("live", client=exchange)

    assert second["checked"] == 0
    assert len(fill_reconcile.storage.fill_reconciliations("live")) == 1
    assert sum(1 for call in exchange.calls if call[0] == "get_order") == 1


async def test_paper_mode_never_queries_an_exchange():
    exchange = ReadOnlyExchange()
    assert (await reconcile_fills("paper", client=exchange))["checked"] == 0
    assert exchange.calls == []


def test_summarize_trades_sums_commission_per_asset():
    summary = summarize_trades([_trade(1, 5, "1", asset="BNB", commission="0.001"),
                                _trade(2, 5, "1", asset="USDT", commission="0.02")], 5)
    assert summary["qty"] == Decimal("2")
    assert summary["commission"] == {"BNB": "0.001", "USDT": "0.02"}


async def test_expired_market_order_with_partial_execution_is_booked(monkeypatch):
    """An EXPIRED market order that executed part of its qty moved real coins;
    the executed quantity must be booked so the holding stays managed."""
    settings = Settings(_env_file=None, trading_mode="live")
    monkeypatch.setattr(autopilot_module, "get_settings", lambda: settings)
    storage = autopilot_module.storage
    storage.open_position(symbol="BTCUSDT", mode="live", qty=Decimal("1"),
                          entry_price=Decimal("100"), agents=["profitstream_strategy"])

    class _Exchange:
        def generate_client_order_id(self) -> str:
            return "ctm-expired"

        async def place_order(self, **kwargs):
            return Order(symbol="BTCUSDT", side=OrderSide.SELL, type=OrderType.MARKET,
                         quantity=Decimal("1"), client_order_id=kwargs["client_order_id"],
                         status=OrderStatus.EXPIRED, exchange_order_id="77",
                         filled_quantity=Decimal("0.4"), avg_fill_price=Decimal("110"))

    monkeypatch.setattr(autopilot_module, "BinanceUSClient", lambda: _Exchange())
    ap = Autopilot()
    ap.state.mode = "live"

    order = await ap._submit("BTCUSDT", OrderSide.SELL, Decimal("1"), ["profitstream_strategy"])

    assert ap._order_filled(order)
    booked = storage.order_by_client_id("live", "ctm-expired")
    assert booked is not None and Decimal(str(booked["qty"])) == Decimal("0.4")
    assert Decimal(str(storage.get_position("BTCUSDT", mode="live")["qty"])) == Decimal("0.6")
    assert "ctm-expired" not in {i["client_order_id"] for i in storage.pending_order_intents("live")}
