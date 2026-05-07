"""Mean reversion agent — RSI extremes inside Bollinger band envelope."""
from __future__ import annotations

from app.config import get_settings
from app.signals import Signal, SignalAction

from .base import Agent, AgentContext


class MeanReversionAgent(Agent):
    name = "mean_reversion"

    def analyze(self, ctx: AgentContext) -> Signal | None:
        df = ctx.df.dropna()
        if df.empty:
            return None
        s = get_settings()
        last = df.iloc[-1]
        rsi = float(last["rsi_14"])
        close = float(last["close"])
        if rsi < s.rsi_oversold and close <= float(last["bb_lower"]):
            return Signal(
                agent=self.name, symbol=ctx.symbol, timeframe=ctx.timeframe,
                action=SignalAction.BUY, confidence=0.75,
                rationale=f"rsi={rsi:.1f} below lower band",
            )
        if rsi > s.rsi_overbought and close >= float(last["bb_upper"]):
            return Signal(
                agent=self.name, symbol=ctx.symbol, timeframe=ctx.timeframe,
                action=SignalAction.SELL, confidence=0.75,
                rationale=f"rsi={rsi:.1f} above upper band",
            )
        return None
