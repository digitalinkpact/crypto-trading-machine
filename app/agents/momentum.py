"""Momentum agent — MACD histogram acceleration."""
from __future__ import annotations

from app.signals import Signal, SignalAction

from .base import Agent, AgentContext


class MomentumAgent(Agent):
    name = "momentum"

    def analyze(self, ctx: AgentContext) -> Signal | None:
        df = ctx.df.dropna()
        if len(df) < 3:
            return None
        h0, h1, h2 = (float(df.iloc[i]["macd_hist"]) for i in (-3, -2, -1))
        if h0 < h1 < h2 and h2 > 0:
            return Signal(
                agent=self.name, symbol=ctx.symbol, timeframe=ctx.timeframe,
                action=SignalAction.BUY, confidence=0.55,
                rationale=f"macd hist rising {h0:.4f}->{h2:.4f}",
            )
        if h0 > h1 > h2 and h2 < 0:
            return Signal(
                agent=self.name, symbol=ctx.symbol, timeframe=ctx.timeframe,
                action=SignalAction.SELL, confidence=0.55,
                rationale=f"macd hist falling {h0:.4f}->{h2:.4f}",
            )
        return None
