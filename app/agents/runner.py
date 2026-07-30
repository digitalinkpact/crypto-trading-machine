"""Agent runner — fans out across symbols × timeframes and aggregates signals."""
from __future__ import annotations

import asyncio

from app.config import TIMEFRAMES, Timeframe, get_settings
from app.data import OHLCVRepository
from app.exchange.symbol_source import get_symbols
from app.logging_setup import get_logger
from app.regime import RegimeClassifier
from app.signals import Signal, SignalAggregator
from app.storage import storage
from app.ta import add_indicators
from app.trading.strategy import ProfitStreamStrategy

from .base import AgentContext
from .breakout import BreakoutAgent
from .llm_reasoner import LLMReasonerAgent
from .mean_reversion import MeanReversionAgent
from .momentum import MomentumAgent
from .regime_overlay import RegimeOverlayAgent
from .trend_follower import TrendFollowerAgent
from .volatility import VolatilityAgent

log = get_logger(__name__)

# 6 sync agents + 1 async LLM agent = 7
SYNC_AGENTS = [
    TrendFollowerAgent(),
    MeanReversionAgent(),
    BreakoutAgent(),
    MomentumAgent(),
    VolatilityAgent(),
    RegimeOverlayAgent(),
]

LLM_AGENT = LLMReasonerAgent()
AGENTS = [*SYNC_AGENTS, LLM_AGENT]

# Only call the LLM on slow timeframes — preserves rate limits on free tiers
# (GitHub Models, Groq, etc.). High-frequency signals come from rule-based agents.
LLM_TIMEFRAMES = (Timeframe.D1, Timeframe.W1)

# Cap concurrent LLM calls. Free tiers throttle aggressively at higher fan-out.
_LLM_CONCURRENCY = 4

# Minimum candles before indicators are computable. The `ta` ATR/RSI windows
# (14) raise on shorter frames; newly-listed coins are skipped until they have
# enough history.
_MIN_BARS = 30


async def run_all_agents(use_llm: bool = False) -> dict[str, Signal]:
    """Run every agent over every (symbol, timeframe), return aggregated signals.

    `use_llm=False` by default to keep API calls off the default tick.
    The LLM agent is restricted to slow timeframes (`LLM_TIMEFRAMES`) and run
    with bounded concurrency to stay within free-tier rate limits.
    """
    repo = OHLCVRepository()
    classifier = RegimeClassifier()
    raw_signals: list[Signal] = []
    llm_tasks: list[asyncio.Task[Signal | None]] = []
    llm_sem = asyncio.Semaphore(_LLM_CONCURRENCY)
    settings = get_settings()
    mode = "paper" if settings.paper_trading else "live"


    # --- ML model gating for LLM signals ---
    import numpy as np
    ML_MODEL_NAME = "signal_quality_v1"
    ml_confidence_threshold = settings.ml_gate_threshold
    ml_model_artifact = storage.load_model_artifact(ML_MODEL_NAME)
    ml_model = ml_model_artifact["model"] if ml_model_artifact else None

    def _llm_features_from_signal(sig: Signal, ctx: AgentContext) -> np.ndarray:
        # Features must match those in _rows_to_xy in regime/trainer.py
        last = ctx.df.dropna().iloc[-1]
        tf_weight = {
            "1h": 1.0,
            "4h": 1.5,
            "1d": 2.5,
            "1w": 4.0,
        }.get(ctx.timeframe.value, 1.0)
        ema_gap = float(last["ema_20"]) - float(last["ema_50"])
        ema_gap_pct = ema_gap / float(last["close"]) if float(last["close"]) else 0.0
        atr_pct = float(last["atr_14"]) / float(last["close"]) if float(last["close"]) else 0.0
        features = [
            float(sig.confidence),
            atr_pct,
            float(last["rsi_14"]),
            ema_gap_pct,
            1.0,  # agent_count (LLM is always 1)
            tf_weight,
            1.0 if sig.action == "BUY" else 0.0,
        ]
        return np.asarray(features, dtype=float).reshape(1, -1)

    def _llm_gate_threshold_for_action(action: str) -> float:
        return 0.40 if action == "BUY" else 0.50

    async def _llm_call(c: AgentContext) -> Signal | None:
        async with llm_sem:
            try:
                sig = await LLM_AGENT.analyze_async(c)
                if sig is None or ml_model is None:
                    return sig
                # Only allow if ML model predicts high win probability
                features = _llm_features_from_signal(sig, c)
                proba = ml_model.predict_proba(features)[0, 1]
                gate_threshold = _llm_gate_threshold_for_action(sig.action)
                if proba >= gate_threshold:
                    return sig
                else:
                    log.info(
                        "LLM signal for %s/%s filtered by ML model: proba=%.2f < %.2f",
                        c.symbol,
                        c.timeframe.value,
                        proba,
                        gate_threshold,
                    )
                    return None
            except Exception as exc:  # noqa: BLE001
                log.warning("llm agent failed %s/%s: %s", c.symbol, c.timeframe.value, exc)
                return None

    symbols = await get_symbols()

    if settings.profitstream_enabled:
        strategy = ProfitStreamStrategy()
        score_threshold = getattr(settings, "profitstream_score_threshold", 80)
        for symbol in symbols:
            decision = await strategy.analyze_symbol(symbol, mode=mode)
            executed = (
                decision.action.value in ("BUY", "SELL")
                and decision.score >= score_threshold
            )
            reason = "; ".join(decision.reasons) if decision.reasons else "score_pass"
            storage.record_tick_audit(
                mode=mode,
                symbol=symbol,
                timeframe="1m/5m/15m/1h",
                action=decision.action.value,
                score=decision.score,
                executed=executed,
                reason=reason,
                indicators=decision.indicators,
                filters={"score_threshold": score_threshold},
            )
            if executed:
                raw_signals.append(
                    Signal(
                        agent="profitstream_strategy",
                        symbol=symbol,
                        timeframe=Timeframe.H1,
                        action=decision.action,
                        confidence=max(0.0, min(1.0, decision.score / 100.0)),
                        rationale=reason,
                        contributing_agents=("profitstream_strategy",),
                    )
                )

        if raw_signals and not settings.profitstream_use_legacy_agents:
            return SignalAggregator().aggregate(raw_signals)

        if not raw_signals:
            log.warning(
                "ProfitStream produced no BUY/SELL signals; falling back to legacy agents"
            )

    for symbol in symbols:
        for tf in TIMEFRAMES:
            try:
                df = await repo.get(symbol, tf, refresh=False)
            except Exception as exc:  # noqa: BLE001
                log.warning("data fetch failed %s/%s: %s", symbol, tf.value, exc)
                continue
            # Newly-listed coins can have very few candles; the indicator stack
            # (ATR/RSI window=14) raises on short frames. Skip them quietly.
            if df is None or len(df) < _MIN_BARS:
                continue
            try:
                df = add_indicators(df)
            except Exception as exc:  # noqa: BLE001
                log.debug("indicators failed %s/%s: %s", symbol, tf.value, exc)
                continue
            try:
                regime = classifier.classify(df)
            except Exception as exc:  # noqa: BLE001
                log.debug("regime classify failed %s/%s: %s", symbol, tf.value, exc)
                continue
            ctx = AgentContext(symbol=symbol, timeframe=tf, df=df, regime=regime)

            for agent in SYNC_AGENTS:
                try:
                    sig = agent.analyze(ctx)
                except Exception as exc:  # noqa: BLE001
                    log.warning("agent %s failed %s/%s: %s", agent.name, symbol, tf.value, exc)
                    continue
                if sig is not None:
                    raw_signals.append(sig)

            if use_llm and tf in LLM_TIMEFRAMES:
                llm_tasks.append(asyncio.create_task(_llm_call(ctx)))

    if llm_tasks:
        for sig in await asyncio.gather(*llm_tasks):
            if sig is not None:
                raw_signals.append(sig)

    return SignalAggregator().aggregate(raw_signals)
