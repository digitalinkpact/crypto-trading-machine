"""Breakout agent — Donchian-style high/low break of last N bars."""
from __future__ import annotations

from app.config import get_settings
from app.signals import Signal, SignalAction

from .base import Agent, AgentContext


class BreakoutAgent(Agent):
    name = "breakout"

    def analyze(self, ctx: AgentContext) -> Signal | None:
        lookback = get_settings().breakout_lookback
        df = ctx.df.dropna()
        if len(df) < lookback + 1:
            return None
        window = df.iloc[-(lookback + 1) : -1]
        last = df.iloc[-1]
        hi = float(window["high"].max())
        lo = float(window["low"].min())
        close = float(last["close"])

        # Volatility gate — don't trust breakouts during dead-vol chop.
        atr = df["atr_14"]
        if len(atr.dropna()) >= 50:
            atr_ratio = float(atr.iloc[-1]) / float(atr.iloc[-50:-1].mean() or 1)
            if atr_ratio < 0.6:
                return None

        if close > hi:
            return Signal(
                agent=self.name, symbol=ctx.symbol, timeframe=ctx.timeframe,
                action=SignalAction.BUY, confidence=0.65,
                rationale=f"close>{lookback}-bar high={hi:.2f}",
            )
        if close < lo:
            return Signal(
                agent=self.name, symbol=ctx.symbol, timeframe=ctx.timeframe,
                action=SignalAction.SELL, confidence=0.65,
                rationale=f"close<{lookback}-bar low={lo:.2f}",
            )
        return None
