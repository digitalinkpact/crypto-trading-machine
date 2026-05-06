"""Agent runner — fans out across symbols × timeframes and aggregates signals."""
from __future__ import annotations

from app.config import SYMBOLS, TIMEFRAMES
from app.data import OHLCVRepository
from app.logging_setup import get_logger
from app.regime import RegimeClassifier
from app.signals import Signal, SignalAggregator
from app.ta import add_indicators

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


async def run_all_agents(use_llm: bool = False) -> dict[str, Signal]:
    """Run every agent over every (symbol, timeframe), return aggregated signals.

    `use_llm=False` by default to keep API calls off the default tick.
    """
    repo = OHLCVRepository()
    classifier = RegimeClassifier()
    raw_signals: list[Signal] = []

    for symbol in SYMBOLS:
        for tf in TIMEFRAMES:
            try:
                df = await repo.get(symbol, tf, refresh=False)
            except Exception as exc:  # noqa: BLE001
                log.warning("data fetch failed %s/%s: %s", symbol, tf.value, exc)
                continue
            df = add_indicators(df)
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

            if use_llm:
                try:
                    sig = await LLM_AGENT.analyze_async(ctx)
                except Exception as exc:  # noqa: BLE001
                    log.warning("llm agent failed %s/%s: %s", symbol, tf.value, exc)
                    sig = None
                if sig is not None:
                    raw_signals.append(sig)

    return SignalAggregator().aggregate(raw_signals)
