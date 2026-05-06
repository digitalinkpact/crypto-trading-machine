"""HTTP routes — read-only telemetry + manual triggers."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.agents import run_all_agents
from app.config import SYMBOLS, TIMEFRAMES, get_settings
from app.signals import Signal

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/config")
async def config_summary() -> dict:
    s = get_settings()
    return {
        "env": s.env,
        "dry_run": s.dry_run,
        "paper_trading": s.paper_trading,
        "symbols": list(SYMBOLS),
        "timeframes": [tf.value for tf in TIMEFRAMES],
        "risk": {
            "max_position_pct": s.max_position_pct,
            "max_portfolio_risk_pct": s.max_portfolio_risk_pct,
            "kelly_fraction_cap": s.kelly_fraction_cap,
        },
    }


@router.post("/agents/tick")
async def agents_tick(use_llm: bool = Query(False)) -> dict[str, Signal]:
    """Run all agents once and return aggregated signals."""
    return await run_all_agents(use_llm=use_llm)
