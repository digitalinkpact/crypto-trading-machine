"""Trend-following agent — EMA20 vs EMA50 cross with EMA200 filter."""
from __future__ import annotations

from app.signals import Signal, SignalAction

from .base import Agent, AgentContext


class TrendFollowerAgent(Agent):
    name = "trend_follower"

    def analyze(self, ctx: AgentContext) -> Signal | None:
        df = ctx.df.dropna()
        if len(df) < 2:
            return None
        last, prev = df.iloc[-1], df.iloc[-2]
        bullish = last["ema_20"] > last["ema_50"] and last["close"] > last["ema_200"]
        bearish = last["ema_20"] < last["ema_50"] and last["close"] < last["ema_200"]
        crossed_up = prev["ema_20"] <= prev["ema_50"] and last["ema_20"] > last["ema_50"]
        crossed_dn = prev["ema_20"] >= prev["ema_50"] and last["ema_20"] < last["ema_50"]

        if crossed_up and bullish:
            action, conf = SignalAction.BUY, 0.7
        elif crossed_dn and bearish:
            action, conf = SignalAction.SELL, 0.7
        elif bullish:
            action, conf = SignalAction.BUY, 0.4
        elif bearish:
            action, conf = SignalAction.SELL, 0.4
        else:
            return None

        return Signal(
            agent=self.name,
            symbol=ctx.symbol,
            timeframe=ctx.timeframe,
            action=action,
            confidence=conf,
            rationale=f"ema20={last['ema_20']:.2f} ema50={last['ema_50']:.2f}",
        )
