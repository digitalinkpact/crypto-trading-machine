from __future__ import annotations

import pandas as pd

from app.signals import SignalAction
from app.trading.strategy import ProfitStreamStrategy


def _frame(*, close: float, rsi: float, bb_lower: float, bb_mid: float, ema50: float, ema200: float, quote_volume: float = 1000.0, ema20: float | None = None, macd_hist: list[float] | None = None) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=80, freq="1D", tz="UTC")
    data = {
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
    }
    if macd_hist is not None:
        # Last N values override the flat tail so bar-over-bar comparisons
        # (e.g. momentum deterioration) have something meaningful to read.
        series = [0.0] * (80 - len(macd_hist)) + list(macd_hist)
        data["macd_hist"] = series
    return pd.DataFrame(data, index=idx)


async def test_entry_strategy_switch_preserves_dip_variant_label(monkeypatch):
    """Both configured dip variants retain their forensic entry label."""
    import app.trading.strategy as strategy_module
    from app.config import get_settings as real_get_settings

    strategy = ProfitStreamStrategy()
    idx = pd.date_range("2026-01-01", periods=80, freq="1D", tz="UTC")
    close = 95.0
    lows = [90.0] * 75 + [85.0, 86.0, 87.0, 88.0, 89.0]  # 5-day low = 85
    eth_df = pd.DataFrame(
        {
            "open": [close] * 80, "high": [close] * 80, "low": lows, "close": [close] * 80,
            "volume": [10.0] * 80, "quote_volume": [1000.0] * 80,
                "rsi_14": [25.0] * 80, "bb_lower": [96.0] * 80, "bb_mid": [105.0] * 80,
            "ema_20": [close] * 80, "ema_50": [100.0] * 80, "ema_200": [95.0] * 80,
        },
        index=idx,
    )
    btc_df = _frame(close=100000, rsi=55, bb_lower=95000, bb_mid=98000, ema50=99000, ema200=97000)

    async def _candles(self, symbol, interval, limit):
        return eth_df if symbol == "ETHUSDT" else btc_df

    monkeypatch.setattr(ProfitStreamStrategy, "_candles", _candles, raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_spread_pct", lambda *_a, **_k: __import__("asyncio").sleep(0, result=0.001), raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_near_news_event", lambda *_a, **_k: (False, ""), raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_is_held", lambda *_a, **_k: False, raising=True)

    real = real_get_settings()
    monkeypatch.setattr(strategy_module, "get_settings", lambda: real.model_copy(update={"entry_strategy": "dip_buy"}))
    decision_dip = await strategy.analyze_symbol("ETHUSDT", mode="paper")
    assert decision_dip.action == SignalAction.BUY
    assert decision_dip.indicators["entry_strategy"] == "dip_buy"

    monkeypatch.setattr(strategy_module, "get_settings", lambda: real.model_copy(update={"entry_strategy": "oversold_bounce"}))
    decision_bounce = await strategy.analyze_symbol("ETHUSDT", mode="paper")
    assert decision_bounce.action == SignalAction.BUY
    assert decision_bounce.indicators["entry_strategy"] == "oversold_bounce"


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


async def test_profitstream_btc_risk_off_is_soft_penalty(monkeypatch):
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

    assert decision.action == SignalAction.BUY
    assert "btc_trend_not_aligned_soft" in decision.reasons


async def test_profitstream_buys_pullback_and_keeps_spread_filter(monkeypatch):
    strategy = ProfitStreamStrategy()
    frames = {
        ("ETHUSDT", "1d"): _frame(
            close=107,
            rsi=70,
            bb_lower=90,
            bb_mid=98,
            ema20=100,
            ema50=95,
            ema200=90,
        ),
        ("BTCUSDT", "1d"): _frame(
            close=90000,
            rsi=55,
            bb_lower=85000,
            bb_mid=88000,
            ema50=87000,
            ema200=97000,
        ),
    }

    async def _candles(self, symbol: str, interval: str, limit: int):
        return frames[(symbol, interval)]

    async def _tight_spread(*_args, **_kwargs):
        return 0.01

    monkeypatch.setattr(ProfitStreamStrategy, "_candles", _candles, raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_spread_pct", _tight_spread, raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_near_news_event", lambda *_a, **_k: (False, ""), raising=True)
    monkeypatch.setattr(ProfitStreamStrategy, "_is_held", lambda *_a, **_k: False, raising=True)

    decision = await strategy.analyze_symbol("ETHUSDT", mode="live")

    assert decision.action == SignalAction.BUY
    assert decision.indicators["entry_strategy"] == "pullback"
    assert decision.indicators["pullback_ready"] is True
    assert "btc_trend_not_aligned_soft" in decision.reasons

    async def _wide_spread(*_args, **_kwargs):
        return 0.011

    monkeypatch.setattr(ProfitStreamStrategy, "_spread_pct", _wide_spread, raising=True)
    blocked = await strategy.analyze_symbol("ETHUSDT", mode="live")

    assert blocked.action == SignalAction.HOLD
    assert any(reason.startswith("spread_wide:") for reason in blocked.reasons)

    frames[("ETHUSDT", "1d")] = _frame(
        close=90,
        rsi=25,
        bb_lower=95,
        bb_mid=105,
        ema20=100,
        ema50=95,
        ema200=90,
    )

    async def _dip_wide_spread(*_args, **_kwargs):
        return 0.003

    monkeypatch.setattr(ProfitStreamStrategy, "_spread_pct", _dip_wide_spread, raising=True)
    dip_blocked = await strategy.analyze_symbol("ETHUSDT", mode="live")

    assert dip_blocked.action == SignalAction.HOLD
    assert "spread_wide:0.3000%>0.2500%" in dip_blocked.reasons


async def test_profitstream_exits_held_position_on_daily_mean_reversion(monkeypatch):
    strategy = ProfitStreamStrategy()
    frames = {
        # +1% unrealized (below trailing_activation_pct=2%, so not deferred to
        # the risk ladder) with close(101) below ema_20(105) -> bearish price
        # confirmation. No macd_hist column -> momentum confirmation is False,
        # so this specifically exercises the price-confirmation branch.
        ("ETHUSDT", "1d"): _frame(close=101, rsi=60, bb_lower=95, bb_mid=105, ema50=100, ema200=95, ema20=105),
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
    assert decision.indicators["exit_reason"] == "mean_reversion_rsi_price"


async def test_profitstream_rsi_recovery_at_breakeven_without_confirmation_does_not_exit(monkeypatch):
    """RSI recovery + breakeven PnL alone is still not enough — real trade
    history shows that combination has a near-zero win rate. Without momentum
    OR price confirmation, the position must be left open (risk ladder keeps
    managing it), not closed on RSI alone."""
    strategy = ProfitStreamStrategy()
    frames = {
        # Breakeven (close==entry==100), price at/above its own EMA20 (no
        # bearish confirmation), no macd_hist column (no momentum signal).
        ("ETHUSDT", "1d"): _frame(close=100, rsi=60, bb_lower=95, bb_mid=105, ema50=100, ema200=95, ema20=95),
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

    assert decision.action == SignalAction.HOLD
    assert any("mean_reversion_exit_awaiting_confirmation" in r for r in decision.reasons)


async def test_profitstream_defers_to_risk_ladder_on_strong_profitable_trend(monkeypatch):
    """Once a position has run up into trailing-stop territory, TP1/TP2/
    trailing should stay in control instead of RSI closing it early."""
    strategy = ProfitStreamStrategy()
    frames = {
        # +15% unrealized, well past trailing_activation_pct (2% default).
        ("ETHUSDT", "1d"): _frame(close=115, rsi=60, bb_lower=95, bb_mid=105, ema50=100, ema200=95, ema20=105),
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

    assert decision.action == SignalAction.HOLD
    assert any("mean_reversion_exit_deferred_to_risk_ladder" in r for r in decision.reasons)


async def test_profitstream_exits_on_momentum_confirmation_alone(monkeypatch):
    """RSI recovery + breakeven + MACD-histogram deterioration (no bearish
    price confirmation) is also sufficient — the two confirmations are an OR,
    not an AND."""
    strategy = ProfitStreamStrategy()
    frames = {
        # Price still at/above EMA20 (no price confirmation), but the MACD
        # histogram's last bar is lower than the prior bar (momentum fading).
        ("ETHUSDT", "1d"): _frame(
            close=101, rsi=60, bb_lower=95, bb_mid=105, ema50=100, ema200=95,
            ema20=95, macd_hist=[0.6, 0.3],
        ),
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
    assert decision.indicators["exit_reason"] == "mean_reversion_rsi_momentum"


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