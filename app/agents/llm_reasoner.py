"""LLM reasoner agent — async wrapper that builds a compact prompt and parses JSON.

This is the only agent that must be invoked async. It is NOT used inside the
order-placement code path; it runs in the scheduler tick alongside the others.
"""
from __future__ import annotations

from app.config import get_settings
from app.llm import LLMReasoner
from app.llm.web_context import get_symbol_web_context
from app.signals import Signal, SignalAction

from .base import AgentContext

_SYSTEM = (
    "You are a quantitative crypto analyst. Given a snapshot of indicators, "
    "respond with a single JSON object: "
    '{"action": "BUY"|"SELL"|"HOLD", "confidence": 0.0-1.0, "rationale": short string}.'
)


class LLMReasonerAgent:
    """Async-only agent. Not derived from the sync `Agent` base class."""

    name = "llm_reasoner"

    def __init__(self, reasoner: LLMReasoner | None = None) -> None:
        self._reasoner = reasoner or LLMReasoner()

    async def analyze_async(self, ctx: AgentContext) -> Signal | None:
        df = ctx.df.dropna()
        if df.empty:
            return None
        last = df.iloc[-1]
        user = (
            f"symbol={ctx.symbol} tf={ctx.timeframe.value} regime={ctx.regime.value}\n"
            f"close={float(last['close']):.4f} rsi14={float(last['rsi_14']):.2f} "
            f"ema20={float(last['ema_20']):.4f} ema50={float(last['ema_50']):.4f} "
            f"ema200={float(last['ema_200']):.4f} "
            f"macd_hist={float(last['macd_hist']):.6f} atr14={float(last['atr_14']):.4f}"
        )
        if get_settings().llm_web_enabled:
            web_ctx = await get_symbol_web_context(ctx.symbol)
            if web_ctx:
                user = f"{user}\nweb_context:\n{web_ctx}"
        out = await self._reasoner.reason(_SYSTEM, user)
        try:
            action = SignalAction(out.get("action", "HOLD"))
        except ValueError:
            action = SignalAction.HOLD
        confidence = float(out.get("confidence", 0.0) or 0.0)
        confidence = max(0.0, min(1.0, confidence))
        return Signal(
            agent=self.name,
            symbol=ctx.symbol,
            timeframe=ctx.timeframe,
            action=action,
            confidence=confidence,
            rationale=str(out.get("rationale", ""))[:200],
            contributing_agents=(self.name,),
        )
