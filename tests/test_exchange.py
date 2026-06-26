"""Exchange wrapper tests — the Binance Spot client is mocked. NEVER hit the real API."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.config import Settings
from app.exchange import BinanceUSClient, OrderSide, OrderStatus, OrderType


@pytest.fixture
def client(monkeypatch):
    # live_mode=False is pinned explicitly: a real .env with LIVE_MODE=true
    # would otherwise trip the model_validator and force dry_run/paper_trading
    # off, making this safety test exercise the live order path by accident.
    settings = Settings(dry_run=True, paper_trading=True, live_mode=False)
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


@pytest.mark.asyncio
async def test_trade_fees_prefers_commission_rates(client):
    client._spot.account.return_value = {
        "commissionRates": {"maker": "0.001", "taker": "0.001"},
        "makerCommission": 40,
        "takerCommission": 60,
    }
    fees = await client.trade_fees()
    assert fees["maker"] == Decimal("0.001")
    assert fees["taker"] == Decimal("0.001")


@pytest.mark.asyncio
async def test_trade_fees_falls_back_to_integer_commission(client):
    client._spot.account.return_value = {
        "makerCommission": 40,   # 40 / 10000 = 0.40%
        "takerCommission": 60,   # 60 / 10000 = 0.60%
    }
    fees = await client.trade_fees()
    assert fees["maker"] == Decimal("0.004")
    assert fees["taker"] == Decimal("0.006")


@pytest.mark.asyncio
async def test_trade_fees_raises_when_missing(client):
    client._spot.account.return_value = {"balances": []}
    with pytest.raises(RuntimeError):
        await client.trade_fees()


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


@pytest.mark.asyncio
async def test_fetch_liquid_universe_filters_and_caps(monkeypatch):
    import pandas as pd
    from app.exchange import symbols as sym_mod

    bases = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    exchange_info = {
        "symbols": [{"symbol": f"{b}USDT", "status": "TRADING"} for b in bases]
    }
    # Volume desc: EEE > DDD > CCC > BBB > AAA; AAA below the $5M floor.
    ticker_24hr = [
        {"symbol": "AAAUSDT", "quoteVolume": "1000000"},     # dropped: < $5M
        {"symbol": "BBBUSDT", "quoteVolume": "6000000"},
        {"symbol": "CCCUSDT", "quoteVolume": "7000000"},
        {"symbol": "DDDUSDT", "quoteVolume": "8000000"},
        {"symbol": "EEEUSDT", "quoteVolume": "9000000"},
    ]

    def _factory(*args, **kwargs):
        return _FakeAsyncClient(exchange_info, ticker_24hr, *args, **kwargs)

    monkeypatch.setattr(sym_mod.httpx, "AsyncClient", _factory)

    class _FakeClient:
        # EEE has a wide 0.50% spread; DDD is too new (5 daily candles).
        spreads = {"BBBUSDT": (100.0, 100.05), "CCCUSDT": (100.0, 100.1),
                   "DDDUSDT": (100.0, 100.1), "EEEUSDT": (100.0, 100.5)}
        ages = {"BBBUSDT": 30, "CCCUSDT": 30, "DDDUSDT": 5, "EEEUSDT": 30}

        async def order_book(self, symbol, limit=5):
            bid, ask = self.spreads[symbol]
            return {"bids": [[str(bid), "10"]], "asks": [[str(ask), "10"]]}

        async def klines(self, symbol, timeframe, limit=500):
            n = min(self.ages[symbol], limit)
            return pd.DataFrame({"close": list(range(n))})

    sym_mod._LIQUID_CACHE = {"symbols": None, "timestamp": 0.0}
    monkeypatch.setattr(
        sym_mod, "get_settings",
        lambda: Settings(
            liquidity_pairlist_enabled=True, universe_size=5,
            min_24h_volume=5_000_000.0, max_spread_percent=0.20,
            min_days_listed=15, final_pairlist_size=10,
        ),
    )

    result = await sym_mod.fetch_liquid_universe(client=_FakeClient())
    # AAA (low vol), DDD (too new), EEE (spread 0.50% > 0.20%) all dropped.
    assert result == ["BBBUSDT", "CCCUSDT"]


@pytest.mark.parametrize(
    "symbol",
    [
        "USD1USDT",    # World Liberty peg that leaked through before the fix
        "USDUCUSDT",   # another newer dollar peg
        "USDCUSDT",
        "USDDUSDT",
        "USDXUSDT",
        "TUSDUSDT",
        "PYUSDUSDT",
        "FDUSDUSDT",
        "DAIUSDT",
        "EURUSDT",
    ],
)
def test_is_stable_pair_excludes_pegs(symbol):
    from app.exchange.symbols import _is_stable_pair

    assert _is_stable_pair(symbol) is True


@pytest.mark.parametrize("symbol", ["BTCUSDT", "ETHUSDT", "SOLUSDT", "HYPEUSDT"])
def test_is_stable_pair_keeps_real_coins(symbol):
    from app.exchange.symbols import _is_stable_pair

    assert _is_stable_pair(symbol) is False


