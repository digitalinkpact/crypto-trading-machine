"""Agent base class and shared context."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import pandas as pd

from app.config import Timeframe
from app.regime import Regime
from app.signals import Signal


@dataclass(frozen=True)
class AgentContext:
    symbol: str
    timeframe: Timeframe
    df: pd.DataFrame  # OHLCV with indicators applied
    regime: Regime


class Agent(ABC):
    """Stateless analyzer: given context, produce zero-or-one Signal."""

    name: str = "agent"

    @abstractmethod
    def analyze(self, ctx: AgentContext) -> Signal | None:
        ...
