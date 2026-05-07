"""Volatility agent — ATR contraction/expansion gate."""
from __future__ import annotations

from app.config import get_settings
from app.signals import Signal, SignalAction

from .base import Agent, AgentContext


class VolatilityAgent(Agent):
    name = "volatility"

    def analyze(self, ctx: AgentContext) -> Signal | None:
        df = ctx.df.dropna()
        if len(df) < 50:
            return None
        atr = df["atr_14"]
        recent = float(atr.iloc[-1])
        baseline = float(atr.iloc[-50:-1].mean())
        if baseline == 0:
            return None
        ratio = recent / baseline
        threshold = get_settings().vol_contraction_threshold
        # Contraction → impending move; bias direction with ema slope.
        if ratio < threshold:
            slope = float(df.iloc[-1]["ema_20"] - df.iloc[-5]["ema_20"])
            action = SignalAction.BUY if slope > 0 else SignalAction.SELL
            return Signal(
                agent=self.name, symbol=ctx.symbol, timeframe=ctx.timeframe,
                action=action, confidence=0.55,
                rationale=f"atr contraction ratio={ratio:.2f}",
            )
        # Strong expansion + trending → follow-through.
        if ratio > 1.5:
            slope = float(df.iloc[-1]["ema_20"] - df.iloc[-5]["ema_20"])
            if abs(slope) > 0:
                action = SignalAction.BUY if slope > 0 else SignalAction.SELL
                return Signal(
                    agent=self.name, symbol=ctx.symbol, timeframe=ctx.timeframe,
                    action=action, confidence=0.45,
                    rationale=f"atr expansion ratio={ratio:.2f}",
                )
        return None
