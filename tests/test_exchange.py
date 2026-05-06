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
