from __future__ import annotations

import pandas as pd

from app.signals import SignalAction
from app.trading.strategy import ProfitStreamStrategy


def _frame(*, close: float, rsi: float, bb_lower: float, bb_mid: float, ema50: float, ema200: float, quote_volume: float = 1000.0, ema20: float | None = None) -> pd.DataFrame:
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
            "ema_20": [ema20 if ema20 is not None else close] * 80,
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
    monkeypatch.setattr(ProfitStreamStrategy, "_held_position", lambda *_a, **_k: {"entry_price": 100.0}, raising=True)

    decision = await strategy.analyze_symbol("ETHUSDT", mode="paper")

    assert decision.action == SignalAction.SELL
    assert decision.indicators["decision"] == "sell_mean_reversion_exit"


async def test_profitstream_suppresses_mean_reversion_exit_while_at_a_loss(monkeypatch):
    """Evidence (scripts/daily_forensic_report.py): the RSI-recovery exit was
    the worst-performing exit path in real live trading (~8.8% win rate over
    90d) because it closes regardless of price. It must not fire while the
    position is still below its entry price — the risk ladder (stop-loss/
    trailing/stale-exit) should manage the downside instead."""
    strategy = ProfitStreamStrategy()
    frames = {
        ("ETHUSDT", "1d"): _frame(close=90, rsi=60, bb_lower=80, bb_mid=85, ema50=100, ema200=95),
        ("BTCUSDT", "1d"): _frame(close=100000, rsi=55, bb_lower=95000, bb_mid=98000, ema50=99000, ema200=97000),
    }

    async def _candles(self, symbol: str, interval: str, limit: int):
        return frames[(symbol, interval)]

    monkeypatch.setattr(ProfitStreamStrategy, "_candles", _candles, raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_spread_pct", lambda *_a, **_k: __import__("asyncio").sleep(0, result=0.001), raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_near_news_event", lambda *_a, **_k: (False, ""), raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_is_held", lambda *_a, **_k: True, raising=True)
    # Entry was 100, close is 90 -> -10% unrealized, below the 0% threshold.
    monkeypatch.setattr(ProfitStreamStrategy, "_held_position", lambda *_a, **_k: {"entry_price": 100.0}, raising=True)

    decision = await strategy.analyze_symbol("ETHUSDT", mode="paper")

    assert decision.action == SignalAction.HOLD
    assert any("mean_reversion_exit_suppressed_at_loss" in r for r in decision.reasons)


async def test_profitstream_rejects_falling_knife_extension(monkeypatch):
    """A dip-buy setup that is WAY below its EMA20 is a capitulation event,
    not a healthy pullback — reject it even though RSI/BB technically qualify."""
    strategy = ProfitStreamStrategy()
    frames = {
        # close=60 is 40% below ema20=100 (default max_dip_extension_pct=15%).
        ("ETHUSDT", "1d"): _frame(close=60, rsi=20, bb_lower=95, bb_mid=105, ema50=100, ema200=95, ema20=100),
        ("BTCUSDT", "1d"): _frame(close=100000, rsi=55, bb_lower=95000, bb_mid=98000, ema50=99000, ema200=97000),
    }

    async def _candles(self, symbol: str, interval: str, limit: int):
        return frames[(symbol, interval)]

    monkeypatch.setattr(ProfitStreamStrategy, "_candles", _candles, raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_spread_pct", lambda *_a, **_k: __import__("asyncio").sleep(0, result=0.001), raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_near_news_event", lambda *_a, **_k: (False, ""), raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_is_held", lambda *_a, **_k: False, raising=True)

    decision = await strategy.analyze_symbol("ETHUSDT", mode="paper")

    assert decision.action == SignalAction.HOLD
    assert any("extension_too_deep_falling_knife" in r for r in decision.reasons)


async def test_profitstream_allows_modest_dip_within_extension_band(monkeypatch):
    """A modest pullback (within max_dip_extension_pct) must still buy."""
    strategy = ProfitStreamStrategy()
    frames = {
        # close=90 is 10% below ema20=100 — inside the default 15% band.
        ("ETHUSDT", "1d"): _frame(close=90, rsi=25, bb_lower=95, bb_mid=105, ema50=100, ema200=95, ema20=100),
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


# ── candidate entry-type scoring (observability only, not live-gating) ─────


def _candidate_frame(n: int = 55, **col_overrides) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=n, freq="1D", tz="UTC")
    base = {
        "close": [100.0] * n,
        "high": [100.0] * n,
        "low": [100.0] * n,
        "volume": [500.0] * n,
        "rsi_14": [50.0] * n,
        "bb_lower": [100.0] * n,
        "ema_20": [100.0] * n,
        "ema_50": [100.0] * n,
        "macd_hist": [0.0] * n,
        "vol_sma_20": [500.0] * n,
    }
    for key, tail_values in col_overrides.items():
        base[key][-len(tail_values):] = tail_values
    return pd.DataFrame(base, index=idx)


def test_oversold_bounce_ready_when_recovering_off_5day_low():
    strategy = ProfitStreamStrategy()
    df = _candidate_frame(
        low=[80, 82, 84, 86, 88],
        close=[82, 84, 86, 90, 94],
        rsi_14=[50, 50, 50, 50, 25],
        bb_lower=[100.0] * 5,
    )
    candidates = strategy._score_entry_candidates(df)
    c = candidates["oversold_bounce"]
    assert c.ready is True
    assert 70 <= c.score <= 85


def test_pullback_to_ema_ready_on_uptrend_pullback():
    strategy = ProfitStreamStrategy()
    df = _candidate_frame(
        ema_20=[100.0] * 5,
        ema_50=[95.0] * 5,
        close=[101.0] * 5,
        rsi_14=[50.0] * 5,
        macd_hist=[0.0, 0.0, 0.0, 0.2, 0.5],
    )
    candidates = strategy._score_entry_candidates(df)
    c = candidates["pullback_to_ema"]
    assert c.ready is True
    assert 75 <= c.score <= 90


def test_breakout_momentum_ready_on_high_break_with_volume():
    strategy = ProfitStreamStrategy()
    df = _candidate_frame(
        high=[100.0] * 20 + [110.0],
        close=[100.0] * 20 + [110.0],
        rsi_14=[50.0] * 20 + [60.0],
        volume=[500.0] * 20 + [1000.0],
        vol_sma_20=[500.0] * 21,
    )
    candidates = strategy._score_entry_candidates(df)
    c = candidates["breakout_momentum"]
    assert c.ready is True
    assert 80 <= c.score <= 95


def test_ma_reversion_ready_near_sma50_with_positive_carryover():
    strategy = ProfitStreamStrategy()
    df = _candidate_frame(
        close=[100.0, 100.1, 100.3, 100.5],
        rsi_14=[50.0, 50.0, 50.0, 40.0],
    )
    candidates = strategy._score_entry_candidates(df)
    c = candidates["ma_reversion"]
    assert c.ready is True
    assert 65 <= c.score <= 80


def test_candidates_not_ready_when_no_setup_present():
    strategy = ProfitStreamStrategy()
    df = _candidate_frame()  # flat, neutral RSI — nothing should qualify
    candidates = strategy._score_entry_candidates(df)
    for name, c in candidates.items():
        assert c.ready is False, name
        assert c.score == 0, name