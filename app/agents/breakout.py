"""Breakout agent — Donchian-style high/low break of last N bars."""
from __future__ import annotations

from app.signals import Signal, SignalAction

from .base import Agent, AgentContext


class BreakoutAgent(Agent):
    name = "breakout"
    lookback: int = 20

    def analyze(self, ctx: AgentContext) -> Signal | None:
        df = ctx.df.dropna()
        if len(df) < self.lookback + 1:
            return None
        window = df.iloc[-(self.lookback + 1) : -1]
        last = df.iloc[-1]
        hi = float(window["high"].max())
        lo = float(window["low"].min())
        close = float(last["close"])

        if close > hi:
            return Signal(
                agent=self.name, symbol=ctx.symbol, timeframe=ctx.timeframe,
                action=SignalAction.BUY, confidence=0.65,
                rationale=f"close>{self.lookback}-bar high={hi:.2f}",
            )
        if close < lo:
            return Signal(
                agent=self.name, symbol=ctx.symbol, timeframe=ctx.timeframe,
                action=SignalAction.SELL, confidence=0.65,
                rationale=f"close<{self.lookback}-bar low={lo:.2f}",
            )
        return None
