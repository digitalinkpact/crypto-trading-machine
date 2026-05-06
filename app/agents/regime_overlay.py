"""Regime overlay — softens or vetoes signals based on current regime."""
from __future__ import annotations

from app.regime import Regime
from app.signals import Signal, SignalAction

from .base import Agent, AgentContext


class RegimeOverlayAgent(Agent):
    name = "regime_overlay"

    def analyze(self, ctx: AgentContext) -> Signal | None:
        if ctx.regime is Regime.BULL:
            return Signal(
                agent=self.name, symbol=ctx.symbol, timeframe=ctx.timeframe,
                action=SignalAction.BUY, confidence=0.3,
                rationale="bull regime bias",
            )
        if ctx.regime is Regime.BEAR:
            return Signal(
                agent=self.name, symbol=ctx.symbol, timeframe=ctx.timeframe,
                action=SignalAction.SELL, confidence=0.3,
                rationale="bear regime bias",
            )
        return Signal(
            agent=self.name, symbol=ctx.symbol, timeframe=ctx.timeframe,
            action=SignalAction.HOLD, confidence=0.3,
            rationale="chop regime — stand down",
        )
