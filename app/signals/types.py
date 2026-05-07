"""Signal types and a weighted-vote aggregator.

The aggregator fuses signals across agents and timeframes per symbol. Two
weight axes:
  - timeframe weight: higher TFs get more weight (1h<4h<1d<1w)
  - agent weight: from Settings, optionally scaled by rolling win-rate

The agent's individual confidence is then multiplied in.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from app.config import Timeframe, get_settings


class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class Signal(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent: str
    symbol: str
    timeframe: Timeframe
    action: SignalAction
    confidence: float = Field(..., ge=0.0, le=1.0)
    rationale: str = ""
    contributing_agents: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# Higher timeframes carry more weight in cross-timeframe fusion.
_TF_WEIGHT: dict[Timeframe, float] = {
    Timeframe.H1: 1.0,
    Timeframe.H4: 1.5,
    Timeframe.D1: 2.5,
    Timeframe.W1: 4.0,
}


def _agent_weight(name: str) -> float:
    s = get_settings()
    table = {
        "trend_follower":  s.agent_weight_trend_follower,
        "mean_reversion":  s.agent_weight_mean_reversion,
        "breakout":        s.agent_weight_breakout,
        "momentum":        s.agent_weight_momentum,
        "volatility":      s.agent_weight_volatility,
        "regime_overlay":  s.agent_weight_regime_overlay,
        "llm_reasoner":    s.agent_weight_llm_reasoner,
    }
    return table.get(name, 1.0)


def _adaptive_multiplier(win_rates: dict[str, float], agent: str) -> float:
    """Scale an agent's vote by its rolling win-rate. Returns 1.0 if no data."""
    if not get_settings().adaptive_agent_weights:
        return 1.0
    wr = win_rates.get(agent)
    if wr is None:
        return 1.0
    # Map win_rate ∈ [0, 1] → multiplier ∈ [0.5, 1.5].
    return max(0.5, min(1.5, 0.5 + wr))


def _load_win_rates() -> dict[str, float]:
    # Lazy import — storage imports config and we want zero cycles.
    if not get_settings().adaptive_agent_weights:
        return {}
    try:
        from app.storage import storage
        return storage.agent_win_rates(min_trades=5)
    except Exception:  # noqa: BLE001
        return {}


class SignalAggregator:
    """Fuses signals per symbol via weighted confidence voting."""

    def aggregate(self, signals: Iterable[Signal]) -> dict[str, Signal]:
        win_rates = _load_win_rates()

        scores: dict[str, dict[SignalAction, float]] = defaultdict(
            lambda: {SignalAction.BUY: 0.0, SignalAction.SELL: 0.0, SignalAction.HOLD: 0.0}
        )
        rationales: dict[str, list[str]] = defaultdict(list)
        contribs: dict[str, dict[SignalAction, set[str]]] = defaultdict(
            lambda: {SignalAction.BUY: set(), SignalAction.SELL: set(), SignalAction.HOLD: set()}
        )
        for s in signals:
            tf_w = _TF_WEIGHT.get(s.timeframe, 1.0)
            agent_w = _agent_weight(s.agent)
            adapt = _adaptive_multiplier(win_rates, s.agent)
            w = tf_w * agent_w * adapt * s.confidence
            scores[s.symbol][s.action] += w
            contribs[s.symbol][s.action].add(s.agent)
            if s.rationale:
                rationales[s.symbol].append(f"[{s.agent}/{s.timeframe.value}] {s.rationale}")

        result: dict[str, Signal] = {}
        for symbol, votes in scores.items():
            action = max(votes, key=votes.__getitem__)
            total = sum(votes.values()) or 1.0
            confidence = min(votes[action] / total, 1.0)
            result[symbol] = Signal(
                agent="aggregator",
                symbol=symbol,
                timeframe=Timeframe.D1,
                action=action,
                confidence=confidence,
                rationale=" | ".join(rationales[symbol]),
                contributing_agents=tuple(sorted(contribs[symbol][action])),
            )
        return result

