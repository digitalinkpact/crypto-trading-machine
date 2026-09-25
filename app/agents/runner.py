"""Agent runner — fans out across symbols × timeframes and aggregates signals."""
from __future__ import annotations

import asyncio
import time

from app.config import TIMEFRAMES, Timeframe, get_settings
from app.data import OHLCVRepository
from app.exchange.symbol_source import get_symbols
from app.exchange.telemetry import exchange_telemetry
from app.logging_setup import get_logger
from app.regime import RegimeClassifier
from app.signals import Signal, SignalAction, SignalAggregator
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

# Minimum candles before indicators are computable (ta ATR/RSI window=14).
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

    # ML quality model gates LLM signals (features must match regime/trainer).
    import numpy as np

    ml_artifact = storage.load_model_artifact("signal_quality_v1")
    ml_model = ml_artifact["model"] if ml_artifact else None

    def _llm_features(sig: Signal, ctx: AgentContext) -> "np.ndarray":
        last = ctx.df.dropna().iloc[-1]
        tf_weight = {"1h": 1.0, "4h": 1.5, "1d": 2.5, "1w": 4.0}.get(ctx.timeframe.value, 1.0)
        close = float(last["close"])
        ema_gap_pct = (float(last["ema_20"]) - float(last["ema_50"])) / close if close else 0.0
        atr_pct = float(last["atr_14"]) / close if close else 0.0
        return np.asarray(
            [float(sig.confidence), atr_pct, float(last["rsi_14"]), ema_gap_pct,
             1.0, tf_weight, 1.0 if sig.action == SignalAction.BUY else 0.0],
            dtype=float,
        ).reshape(1, -1)

    async def _llm_call(c: AgentContext) -> Signal | None:
        async with llm_sem:
            try:
                sig = await LLM_AGENT.analyze_async(c)
                if sig is None or ml_model is None:
                    return sig
                proba = float(ml_model.predict_proba(_llm_features(sig, c))[0, 1])
                gate = 0.40 if sig.action == SignalAction.BUY else 0.50
                if proba >= gate:
                    return sig
                log.info("LLM %s %s filtered by ML gate: proba=%.2f < %.2f",
                         c.symbol, c.timeframe.value, proba, gate)
                return None
            except Exception as exc:  # noqa: BLE001
                log.warning("llm agent failed %s/%s: %s", c.symbol, c.timeframe.value, exc)
                return None

    symbols = await get_symbols()

    # ── ProfitStream — the walk-forward-validated dip-buy / oversold-bounce
    #    strategy. This is the PRIMARY live strategy; the legacy multi-agent
    #    ensemble below is a fallback, reachable only when
    #    profitstream_use_legacy_agents=True.
    if settings.profitstream_enabled:
        started = time.perf_counter()
        strategy = ProfitStreamStrategy()
        score_threshold = int(settings.profitstream_score_threshold)
        try:
            btc_1d = await strategy._candles("BTCUSDT", "1d", 320)
        except Exception as exc:  # noqa: BLE001
            log.warning("ProfitStream BTC context unavailable; skipping strategy pass: %s", exc)
            exchange_telemetry.record_stage("strategy", time.perf_counter() - started)
            return {}
        for symbol in symbols:
            try:
                decision = await strategy.analyze_symbol(symbol, mode=mode, btc_1d=btc_1d)
            except Exception as exc:  # noqa: BLE001
                log.warning("ProfitStream analyze failed %s: %s", symbol, exc)
                continue
            executed = (
                decision.action == SignalAction.SELL
                or (decision.action == SignalAction.BUY and decision.score >= score_threshold)
            )
            reason = "; ".join(decision.reasons) if decision.reasons else "score_pass"
            try:
                storage.record_tick_audit(
                    mode=mode, symbol=symbol, timeframe="1d",
                    action=decision.action.value, score=decision.score,
                    executed=executed, reason=reason,
                    indicators=decision.indicators,
                    filters={"score_threshold": score_threshold},
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("tick_audit record failed %s: %s", symbol, exc)
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
        exchange_telemetry.record_stage("strategy", time.perf_counter() - started)
        if not settings.profitstream_use_legacy_agents:
            # ProfitStream is the SOLE strategy. An all-HOLD tick is the intended,
            # healthy outcome of a strict quality bar — never fall back to the
            # noisier legacy ensemble (2026-07-28 audit: that fallback silently
            # produced most live trades at a 14.6% win rate). Bypass the
            # aggregator too: with ≤1 signal/symbol its weighted-vote
            # renormalization would collapse confidence to 1.0.
            return {sig.symbol: sig for sig in raw_signals}
        if raw_signals:
            return SignalAggregator().aggregate(raw_signals)
        log.warning("ProfitStream produced no signals; falling back to legacy agents "
                    "(profitstream_use_legacy_agents=true)")

    for symbol in symbols:
        for tf in TIMEFRAMES:
            try:
                df = await repo.get(symbol, tf, refresh=False)
            except Exception as exc:  # noqa: BLE001
                log.warning("data fetch failed %s/%s: %s", symbol, tf.value, exc)
                continue
            # Newly-listed coins can have too few candles for the indicator
            # stack (ATR/RSI window=14). Skip them quietly.
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
