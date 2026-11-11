"""Agent runner — fans out across symbols × timeframes and aggregates signals."""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from datetime import datetime, timezone

from app.config import DAILY_FETCH_BARS, TIMEFRAMES, Timeframe, get_settings
from app.data import OHLCVRepository
from app.exchange.symbol_source import get_symbols
from app.logging_setup import get_logger
from app.regime import RegimeClassifier
from app.regime.btc_regime import compute_btc_regime_score
from app.signals import Signal, SignalAggregator
from app.storage import storage
from app.exchange.telemetry import exchange_telemetry
from app.ta import add_indicators
from app.trading.strategy import ProfitStreamStrategy
from app.trading.execution_context import current_execution, record_error

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
    scan_key = "profitstream_llm_scan" if use_llm else "profitstream_scan"


    # --- ML model gating for LLM signals ---
    import numpy as np
    ML_MODEL_NAME = "signal_quality_v1"
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
    if not symbols:
        raise ValueError("No eligible market universe available")

    if settings.profitstream_enabled:
        strategy_started = time.perf_counter()
        strategy = ProfitStreamStrategy()
        configured_threshold = int(getattr(settings, "profitstream_score_threshold", 80))
        score_threshold = configured_threshold
        scan_reasons: Counter[str] = Counter()
        scan_actions: Counter[str] = Counter()
        scan_checks: Counter[str] = Counter()
        usable_symbols = 0
        breadth_usable = 0
        breadth_above = 0
        highest_score = 0
        try:
            btc_1d = await strategy._candles("BTCUSDT", "1d", DAILY_FETCH_BARS)
        except Exception as exc:  # noqa: BLE001
            log.warning("ProfitStream BTC context unavailable; skipping strategy pass: %s", exc)
            record_error("btc_daily_context", exc)
            storage.kv_set(scan_key, {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "timeframe": Timeframe.D1.value,
                "symbols": len(symbols),
                "usable_symbols": 0,
                "highest_score": 0,
                "score_threshold": score_threshold,
                "market_allowed": False,
                "error": str(exc),
            })
            exchange_telemetry.record_stage("strategy", time.perf_counter() - strategy_started)
            return {}
        for symbol in symbols:
            decision = await strategy.analyze_symbol(symbol, mode=mode, btc_1d=btc_1d)
            if any(reason.startswith("data_unavailable:") for reason in decision.reasons):
                record_error(f"market_data:{symbol}", ValueError("daily data unavailable"))
            execution = current_execution.get()
            if execution is not None:
                execution["pairs_evaluated"] += 1
                execution["decisions"][decision.action.value] += 1
            runner_pass = (
                decision.action.value == "SELL"
                or (
                    decision.action.value == "BUY"
                    and decision.score >= score_threshold
                )
            )
            reason = "; ".join(decision.reasons) if decision.reasons else "score_pass"
            scan_actions[decision.action.value] += 1
            scan_reasons.update(set(r.split(":", 1)[0] for r in decision.reasons))
            highest_score = max(highest_score, decision.score)
            checks = decision.indicators.get("entry_checks")
            if checks is not None:
                scan_checks.update({name: int(ok) for name, ok in checks.items()})
            if "rsi_1d" in decision.indicators:
                usable_symbols += 1
                above = decision.indicators.get("above_breadth_ema")
                if symbol != "BTCUSDT" and above is not None:
                    breadth_usable += 1
                    breadth_above += int(above)
            decision_id = storage.record_tick_audit(
                mode=mode,
                symbol=symbol,
                timeframe=Timeframe.D1.value,
                action=decision.action.value,
                score=decision.score,
                executed=False,
                reason=reason,
                indicators=decision.indicators,
                filters={
                    "audit_version": 2,
                    "score_threshold": score_threshold,
                    "strategy_buy": decision.action.value == "BUY",
                    "score_pass": decision.score >= score_threshold,
                    "runner_pass": runner_pass,
                    "submitted": False,
                    "filled": False,
                },
            )
            if runner_pass:
                raw_signals.append(
                    Signal(
                        agent="profitstream_strategy",
                        symbol=symbol,
                        timeframe=Timeframe.D1,
                        action=decision.action,
                        confidence=max(0.0, min(1.0, decision.score / 100.0)),
                        quality_score=decision.score,
                        rationale=reason,
                        contributing_agents=("profitstream_strategy",),
                        exit_reason=str(decision.indicators.get("exit_reason") or ""),
                        entry_strategy=str(decision.indicators.get("entry_strategy") or ""),
                        entry_btc_regime=decision.indicators.get("btc_regime_score"),
                        decision_id=decision_id,
                    )
                )

        regime = compute_btc_regime_score(btc_1d)
        strong_bull_required = getattr(settings, "require_strong_bull_regime", False)
        market_enabled = getattr(settings, "market_regime_gate_enabled", True)
        breadth_expected = sum(symbol != "BTCUSDT" for symbol in symbols)
        breadth_fraction = breadth_above / breadth_usable if breadth_usable else 0.0
        qualified_buy_candidates = sum(
            signal.action.value == "BUY" for signal in raw_signals
        )
        breadth_allowed = not getattr(settings, "breadth_gate_enabled", False) or (
            breadth_usable == breadth_expected
            and breadth_usable >= getattr(settings, "breadth_min_symbols", 15)
            and breadth_fraction >= getattr(settings, "breadth_min_fraction", 0.5)
        )
        storage.kv_set(scan_key, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "timeframe": Timeframe.D1.value,
            "symbols": len(symbols),
            "actions": dict(scan_actions),
            "reasons": dict(scan_reasons),
            "highest_score": highest_score,
            "score_threshold": score_threshold,
            "qualified_buy_candidates": qualified_buy_candidates,
            "candle_close_at": btc_1d.attrs.get("candle_close_at"),
            "usable_symbols": usable_symbols,
            "predicate_pass_counts": dict(scan_checks),
            "btc_regime_score": regime.score,
            "btc_regime_label": regime.label,
            "market_allowed": (
                not market_enabled or regime.score >= (2 if strong_bull_required else 0)
            ),
            "strong_bull_required": strong_bull_required,
            "breadth": {
                "above": breadth_above, "usable": breadth_usable,
                "expected": breadth_expected, "fraction": breadth_fraction,
                "allowed": breadth_allowed,
                "ema_span": getattr(settings, "breadth_ema_span", 20),
            },
        })

        if not settings.profitstream_use_legacy_agents:
            # ProfitStream is the SOLE strategy — do not fall back to the
            # noisier legacy multi-agent ensemble just because this tick
            # produced few or zero qualifying signals. An all-HOLD tick is the
            # intended, healthy outcome of a stricter quality bar, not a
            # trigger to reach for a worse strategy. (Live evidence: 2026-07-28
            # audit found the legacy ensemble was silently producing most live
            # trades — at a 14.6% win rate — precisely because this fallback
            # fired on almost every tick where ProfitStream stayed quiet.)
            # Bypass SignalAggregator here too: with at most one signal per
            # symbol, its weighted-vote renormalization collapses confidence
            # to 1.0 and would destroy the real 0-110 quality_score-derived
            # confidence computed above.
            exchange_telemetry.record_stage("strategy", time.perf_counter() - strategy_started)
            return {sig.symbol: sig for sig in raw_signals}

        if raw_signals:
            return SignalAggregator().aggregate(raw_signals)

        log.warning(
            "ProfitStream produced no BUY/SELL signals; falling back to legacy agents "
            "(profitstream_use_legacy_agents=true)"
        )
        exchange_telemetry.record_stage("strategy", time.perf_counter() - strategy_started)

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
