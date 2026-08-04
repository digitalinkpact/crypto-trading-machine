from __future__ import annotations

import pandas as pd

from app.signals import SignalAction
from app.trading.strategy import ProfitStreamStrategy


def _frame(*, close: float, rsi: float, bb_lower: float, bb_mid: float, ema50: float, ema200: float, quote_volume: float = 1000.0) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=80, freq="1D", tz="UTC")
    return pd.DataFrame(
        {
            "open": [close] * 80,
            "high": [close] * 80,
            "low": [close] * 80,
            "close": [close] * 80,
            "volume": [10.0] * 80,
            "quote_volume": [quote_volume] * 80,
            "rsi_14": [rsi] * 80,
            "bb_lower": [bb_lower] * 80,
            "bb_mid": [bb_mid] * 80,
            "ema_50": [ema50] * 80,
            "ema_200": [ema200] * 80,
        },
        index=idx,
    )


async def test_profitstream_buys_daily_dip_with_btc_risk_on(monkeypatch):
    strategy = ProfitStreamStrategy()
    frames = {
        ("ETHUSDT", "1d"): _frame(close=90, rsi=25, bb_lower=95, bb_mid=105, ema50=100, ema200=95),
        ("BTCUSDT", "1d"): _frame(close=100000, rsi=55, bb_lower=95000, bb_mid=98000, ema50=99000, ema200=97000),
    }

    async def _candles(self, symbol: str, interval: str, limit: int):
        return frames[(symbol, interval)]

    monkeypatch.setattr(ProfitStreamStrategy, "_candles", _candles, raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_spread_pct", lambda *_a, **_k: __import__("asyncio").sleep(0, result=0.001), raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_near_news_event", lambda *_a, **_k: (False, ""), raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_is_held", lambda *_a, **_k: False, raising=True)

    decision = await strategy.analyze_symbol("ETHUSDT", mode="paper")

    assert decision.action == SignalAction.BUY
    assert decision.score >= 90


async def test_profitstream_holds_when_btc_risk_off(monkeypatch):
    strategy = ProfitStreamStrategy()
    frames = {
        ("ETHUSDT", "1d"): _frame(close=90, rsi=25, bb_lower=95, bb_mid=105, ema50=100, ema200=95),
        ("BTCUSDT", "1d"): _frame(close=90000, rsi=55, bb_lower=85000, bb_mid=88000, ema50=87000, ema200=97000),
    }

    async def _candles(self, symbol: str, interval: str, limit: int):
        return frames[(symbol, interval)]

    monkeypatch.setattr(ProfitStreamStrategy, "_candles", _candles, raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_spread_pct", lambda *_a, **_k: __import__("asyncio").sleep(0, result=0.001), raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_near_news_event", lambda *_a, **_k: (False, ""), raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_is_held", lambda *_a, **_k: False, raising=True)

    decision = await strategy.analyze_symbol("ETHUSDT", mode="paper")

    assert decision.action == SignalAction.HOLD
    assert "btc_trend_not_aligned" in decision.reasons


async def test_profitstream_exits_held_position_on_daily_mean_reversion(monkeypatch):
    strategy = ProfitStreamStrategy()
    frames = {
        ("ETHUSDT", "1d"): _frame(close=110, rsi=60, bb_lower=95, bb_mid=105, ema50=100, ema200=95),
        ("BTCUSDT", "1d"): _frame(close=100000, rsi=55, bb_lower=95000, bb_mid=98000, ema50=99000, ema200=97000),
    }

    async def _candles(self, symbol: str, interval: str, limit: int):
        return frames[(symbol, interval)]

    monkeypatch.setattr(ProfitStreamStrategy, "_candles", _candles, raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_spread_pct", lambda *_a, **_k: __import__("asyncio").sleep(0, result=0.001), raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_near_news_event", lambda *_a, **_k: (False, ""), raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_is_held", lambda *_a, **_k: True, raising=True)

    decision = await strategy.analyze_symbol("ETHUSDT", mode="paper")

    assert decision.action == SignalAction.SELL
    assert decision.indicators["decision"] == "sell_mean_reversion_exit"