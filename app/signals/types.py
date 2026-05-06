"""Signal types and a weighted-vote aggregator."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from app.config import Timeframe


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
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# Higher timeframes carry more weight in cross-timeframe fusion.
_TF_WEIGHT: dict[Timeframe, float] = {
    Timeframe.H1: 1.0,
    Timeframe.H4: 1.5,
    Timeframe.D1: 2.5,
    Timeframe.W1: 4.0,
}


class SignalAggregator:
    """Fuses signals per symbol via weighted confidence voting."""

    def aggregate(self, signals: Iterable[Signal]) -> dict[str, Signal]:
        scores: dict[str, dict[SignalAction, float]] = defaultdict(
            lambda: {SignalAction.BUY: 0.0, SignalAction.SELL: 0.0, SignalAction.HOLD: 0.0}
        )
        rationales: dict[str, list[str]] = defaultdict(list)
        for s in signals:
            w = _TF_WEIGHT.get(s.timeframe, 1.0) * s.confidence
            scores[s.symbol][s.action] += w
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
            )
        return result
