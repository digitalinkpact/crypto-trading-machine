"""Exchange wrapper tests — the Binance Spot client is mocked. NEVER hit the real API."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.exchange import BinanceUSClient, OrderSide, OrderStatus, OrderType


@pytest.fixture
def client(monkeypatch):
    settings = Settings(dry_run=True, paper_trading=True)
    c = BinanceUSClient(settings=settings)
    c._spot = MagicMock()  # ensure no network call is possible
    return c


@pytest.mark.asyncio
async def test_dry_run_does_not_call_exchange(client):
    order = await client.place_order(
        "BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("0.001")
    )
    assert order.status is OrderStatus.DRY_RUN
    assert order.client_order_id.startswith("ctm-")
    client._spot.new_order.assert_not_called()


@pytest.mark.asyncio
async def test_live_order_uses_client_order_id(monkeypatch):
    settings = Settings(dry_run=False, paper_trading=False)
    c = BinanceUSClient(settings=settings)
    c._spot = MagicMock()
    c._spot.new_order.return_value = {
        "status": "FILLED",
        "orderId": 42,
        "executedQty": "0.001",
    }
    order = await c.place_order(
        "BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("0.001"),
        client_order_id="ctm-test-123",
    )
    assert order.status is OrderStatus.FILLED
    kwargs = c._spot.new_order.call_args.kwargs
    assert kwargs["newClientOrderId"] == "ctm-test-123"
    assert kwargs["symbol"] == "BTCUSDT"


@pytest.mark.asyncio
async def test_live_order_sets_avg_fill_price_from_fills():
    settings = Settings(dry_run=False, paper_trading=False)
    c = BinanceUSClient(settings=settings)
    c._spot = MagicMock()
    c._spot.new_order.return_value = {
        "status": "FILLED",
        "orderId": 99,
        "executedQty": "2",
        "fills": [
            {"price": "100", "qty": "1"},
            {"price": "102", "qty": "1"},
        ],
    }
    order = await c.place_order(
        "BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("2"),
        client_order_id="ctm-test-fills",
    )
    assert order.avg_fill_price == Decimal("101")


@pytest.mark.asyncio
async def test_live_order_sets_avg_fill_price_from_cum_quote():
    settings = Settings(dry_run=False, paper_trading=False)
    c = BinanceUSClient(settings=settings)
    c._spot = MagicMock()
    c._spot.new_order.return_value = {
        "status": "FILLED",
        "orderId": 100,
        "executedQty": "2",
        "cummulativeQuoteQty": "202",
    }
    order = await c.place_order(
        "BTCUSDT", OrderSide.BUY, OrderType.MARKET, Decimal("2"),
        client_order_id="ctm-test-cumquote",
    )
    assert order.avg_fill_price == Decimal("101")


def _filters_with(step: str, min_qty: str = "0"):
    from app.exchange.filters import SymbolFilters

    f = SymbolFilters()
    f._info = {
        "X": {"status": "TRADING", "step_size": Decimal(step), "min_qty": Decimal(min_qty)}
    }
    return f


@pytest.mark.parametrize(
    "step, raw, expected",
    [
        ("1", "500000.9", "500000"),       # large qty must NOT become "5E+5"
        ("0.00001", "0.00018837", "0.00018"),
        ("0.1", "5.92", "5.9"),
        ("1", "3", "3"),
    ],
)
def test_round_qty_never_scientific(step, raw, expected):
    f = _filters_with(step)
    q = f.round_qty("X", Decimal(raw))
    assert q == Decimal(expected)
    # Binance rejects scientific notation with -1100; the string form must be plain.
    assert "E" not in str(q) and "e" not in str(q)


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stubs httpx.AsyncClient for fetch_dynamic_symbols — no network."""

    def __init__(self, exchange_info, ticker_24hr, *args, **kwargs):
        self._exchange_info = exchange_info
        self._ticker_24hr = ticker_24hr

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, *args, **kwargs):
        if "exchangeInfo" in url:
            return _FakeResp(self._exchange_info)
        if "ticker/24hr" in url:
            return _FakeResp(self._ticker_24hr)
        raise AssertionError(f"unexpected url {url}")


@pytest.mark.asyncio
async def test_fetch_dynamic_symbols_caps_to_top_n_by_volume(monkeypatch):
    from app.exchange import symbols as sym_mod

    bases = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    exchange_info = {
        "symbols": [
            {"symbol": f"{b}USDT", "status": "TRADING"} for b in bases
        ]
    }
    # Volumes ascending so AAA is least and EEE is most liquid.
    ticker_24hr = [
        {"symbol": "AAAUSDT", "quoteVolume": "100"},
        {"symbol": "BBBUSDT", "quoteVolume": "200"},
        {"symbol": "CCCUSDT", "quoteVolume": "300"},
        {"symbol": "DDDUSDT", "quoteVolume": "400"},
        {"symbol": "EEEUSDT", "quoteVolume": "500"},
    ]

    def _factory(*args, **kwargs):
        return _FakeAsyncClient(exchange_info, ticker_24hr, *args, **kwargs)

    monkeypatch.setattr(sym_mod.httpx, "AsyncClient", _factory)
    # Bust the module-level cache and force a top-3 cap with no volume floor.
    sym_mod._SYMBOLS_CACHE = {"symbols": None, "timestamp": 0.0}
    monkeypatch.setattr(
        sym_mod, "get_settings",
        lambda: Settings(use_dynamic_symbols=True, min_quote_volume_usdt=0.0, max_symbols=3),
    )

    result = await sym_mod.fetch_dynamic_symbols()
    # Top 3 by volume are EEE/DDD/CCC; output is sorted alphabetically.
    assert result == ["CCCUSDT", "DDDUSDT", "EEEUSDT"]

