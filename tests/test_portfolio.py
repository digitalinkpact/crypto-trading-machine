from __future__ import annotations

from decimal import Decimal

from app.trading.portfolio import portfolio_snapshot


class _LiveClient:
    def __init__(self) -> None:
        self.quoted_symbols: list[str] = []

    async def account(self) -> dict:
        return {
            "canTrade": True,
            "accountType": "SPOT",
            "balances": [
                {"asset": "USDT", "free": "100", "locked": "5"},
                {"asset": "USD", "free": "30", "locked": "2"},
                {"asset": "BTC", "free": "0.1", "locked": "0"},
            ],
        }

    async def ticker_price(self, symbol: str) -> Decimal:
        self.quoted_symbols.append(symbol)
        assert symbol == "BTCUSDT"
        return Decimal("50000")


async def test_live_snapshot_includes_usd_and_locked_quote_cash():
    client = _LiveClient()

    snapshot = await portfolio_snapshot(client=client, mode="live")

    assert snapshot["usdt_cash"] == Decimal("100")
    assert snapshot["total_usdt"] == Decimal("5137")
    assert client.quoted_symbols == ["BTCUSDT"]