from __future__ import annotations

from types import SimpleNamespace

import pytest
import pandas as pd

from app.config import Timeframe
from app.signals import Signal, SignalAction


@pytest.mark.asyncio
async def test_run_all_agents_falls_back_when_profitstream_is_empty(monkeypatch):
    from app.agents import runner

    async def _symbols():
        return ["BTCUSDT"]

    class _EmptyProfitStream:
        async def analyze_symbol(self, symbol: str, *, mode: str):
            return SimpleNamespace(
                action=SignalAction.HOLD,
                score=0,
                reasons=["insufficient_history"],
                indicators={"symbol": symbol},
            )

    class _LegacySignalAgent:
        name = "legacy_test_agent"

        def analyze(self, ctx):
            return Signal(
                agent=self.name,
                symbol=ctx.symbol,
                timeframe=ctx.timeframe,
                action=SignalAction.BUY,
                confidence=0.9,
                rationale="fallback path",
                contributing_agents=(self.name,),
            )

    class _DummyRepo:
        async def get(self, symbol, tf, refresh=False):
            idx = pd.date_range("2026-07-20", periods=40, freq="min", tz="UTC")
            return pd.DataFrame(
                {
                    "open": [100.0] * 40,
                    "high": [101.0] * 40,
                    "low": [99.0] * 40,
                    "close": [100.5] * 40,
                    "volume": [10_000.0] * 40,
                },
                index=idx,
            )

    class _DummyClassifier:
        def classify(self, df):
            return "trend"

    class _DummyAggregator:
        def aggregate(self, signals):
            out = {}
            for sig in signals:
                out[sig.symbol] = sig
            return out

    monkeypatch.setattr(runner, "get_symbols", _symbols)
    monkeypatch.setattr(runner, "ProfitStreamStrategy", _EmptyProfitStream)
    monkeypatch.setattr(runner, "OHLCVRepository", _DummyRepo)
    monkeypatch.setattr(runner, "RegimeClassifier", _DummyClassifier)
    monkeypatch.setattr(runner, "SignalAggregator", _DummyAggregator)
    monkeypatch.setattr(runner, "SYNC_AGENTS", [_LegacySignalAgent()])
    monkeypatch.setattr(runner, "LLM_AGENT", _LegacySignalAgent())
    monkeypatch.setattr(runner, "AGENTS", [_LegacySignalAgent()])
    monkeypatch.setattr(runner, "TIMEFRAMES", [Timeframe.D1])
    monkeypatch.setattr(runner, "add_indicators", lambda df: df)

    class _Settings:
        profitstream_enabled = True
        profitstream_use_legacy_agents = False
        ml_gate_threshold = 0.5
        paper_trading = True

    monkeypatch.setattr(runner, "get_settings", lambda: _Settings())

    signals = await runner.run_all_agents(use_llm=False)

    assert "BTCUSDT" in signals
    assert signals["BTCUSDT"].action is SignalAction.BUY


@pytest.mark.asyncio
async def test_run_all_agents_allows_llm_buy_at_lower_threshold(monkeypatch):
    from app.agents import runner

    class _Model:
        def predict_proba(self, _features):
            import numpy as np

            return np.asarray([[0.59, 0.41]])

    async def _symbols():
        return ["BTCUSDT"]

    async def _llm_signal(_ctx):
        return Signal(
            agent="llm_reasoner",
            symbol="BTCUSDT",
            timeframe=Timeframe.D1,
            action=SignalAction.BUY,
            confidence=0.9,
            rationale="buy",
            contributing_agents=("llm_reasoner",),
        )

    monkeypatch.setattr(runner, "get_symbols", _symbols)
    monkeypatch.setattr(runner, "ProfitStreamStrategy", lambda: None)
    monkeypatch.setattr(runner, "OHLCVRepository", lambda: None)
    monkeypatch.setattr(runner, "RegimeClassifier", lambda: None)
    monkeypatch.setattr(runner, "SignalAggregator", lambda: type("Agg", (), {"aggregate": lambda self, sigs: {sig.symbol: sig for sig in sigs}})())
    monkeypatch.setattr(runner, "SYNC_AGENTS", [])
    monkeypatch.setattr(runner, "LLM_TIMEFRAMES", (Timeframe.D1,))
    monkeypatch.setattr(runner, "add_indicators", lambda df: df)

    class _Repo:
        async def get(self, symbol, tf, refresh=False):
            idx = pd.date_range("2026-07-20", periods=40, freq="min", tz="UTC")
            return pd.DataFrame(
                {
                    "open": [100.0] * 40,
                    "high": [101.0] * 40,
                    "low": [99.0] * 40,
                    "close": [100.5] * 40,
                    "volume": [10_000.0] * 40,
                    "ema_20": [100.0] * 40,
                    "ema_50": [99.0] * 40,
                    "atr_14": [1.0] * 40,
                    "rsi_14": [50.0] * 40,
                },
                index=idx,
            )

    class _Classifier:
        def classify(self, df):
            return "trend"

    class _Settings:
        profitstream_enabled = False
        profitstream_use_legacy_agents = False
        ml_gate_threshold = 0.5
        paper_trading = True

    monkeypatch.setattr(runner, "OHLCVRepository", _Repo)
    monkeypatch.setattr(runner, "RegimeClassifier", _Classifier)
    monkeypatch.setattr(runner, "get_settings", lambda: _Settings())
    monkeypatch.setattr(runner.storage, "load_model_artifact", lambda _name: {"model": _Model()})
    monkeypatch.setattr(runner.LLM_AGENT, "analyze_async", _llm_signal)

    signals = await runner.run_all_agents(use_llm=True)

    assert "BTCUSDT" in signals
    assert signals["BTCUSDT"].action is SignalAction.BUY


@pytest.mark.asyncio
async def test_run_all_agents_blocks_llm_sell_at_higher_threshold(monkeypatch):
    from app.agents import runner

    class _Model:
        def predict_proba(self, _features):
            import numpy as np

            return np.asarray([[0.55, 0.45]])

    async def _symbols():
        return ["BTCUSDT"]

    async def _llm_signal(_ctx):
        return Signal(
            agent="llm_reasoner",
            symbol="BTCUSDT",
            timeframe=Timeframe.D1,
            action=SignalAction.SELL,
            confidence=0.9,
            rationale="sell",
            contributing_agents=("llm_reasoner",),
        )

    monkeypatch.setattr(runner, "get_symbols", _symbols)
    monkeypatch.setattr(runner, "ProfitStreamStrategy", lambda: None)
    monkeypatch.setattr(runner, "OHLCVRepository", lambda: None)
    monkeypatch.setattr(runner, "RegimeClassifier", lambda: None)
    monkeypatch.setattr(runner, "SignalAggregator", lambda: type("Agg", (), {"aggregate": lambda self, sigs: {sig.symbol: sig for sig in sigs}})())
    monkeypatch.setattr(runner, "SYNC_AGENTS", [])
    monkeypatch.setattr(runner, "LLM_TIMEFRAMES", (Timeframe.D1,))
    monkeypatch.setattr(runner, "add_indicators", lambda df: df)

    class _Repo:
        async def get(self, symbol, tf, refresh=False):
            idx = pd.date_range("2026-07-20", periods=40, freq="min", tz="UTC")
            return pd.DataFrame(
                {
                    "open": [100.0] * 40,
                    "high": [101.0] * 40,
                    "low": [99.0] * 40,
                    "close": [100.5] * 40,
                    "volume": [10_000.0] * 40,
                    "ema_20": [100.0] * 40,
                    "ema_50": [99.0] * 40,
                    "atr_14": [1.0] * 40,
                    "rsi_14": [50.0] * 40,
                },
                index=idx,
            )

    class _Classifier:
        def classify(self, df):
            return "trend"

    class _Settings:
        profitstream_enabled = False
        profitstream_use_legacy_agents = False
        ml_gate_threshold = 0.5
        paper_trading = True

    monkeypatch.setattr(runner, "OHLCVRepository", _Repo)
    monkeypatch.setattr(runner, "RegimeClassifier", _Classifier)
    monkeypatch.setattr(runner, "get_settings", lambda: _Settings())
    monkeypatch.setattr(runner.storage, "load_model_artifact", lambda _name: {"model": _Model()})
    monkeypatch.setattr(runner.LLM_AGENT, "analyze_async", _llm_signal)

    signals = await runner.run_all_agents(use_llm=True)

    assert signals == {}
