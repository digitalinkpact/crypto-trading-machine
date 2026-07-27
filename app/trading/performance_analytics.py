"""Performance analytics and ranking snapshots for strategy optimization.

Computes portfolio and trade quality KPIs from persisted trade/equity/audit data.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import mean, pstdev

from app.logging_setup import get_logger
from app.storage import storage

log = get_logger(__name__)


def _parse_dt(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _to_float(value) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _max_drawdown(values: list[float]) -> float:
    if not values:
        return 0.0
    peak = values[0]
    max_dd = 0.0
    for v in values:
        if v > peak:
            peak = v
        if peak > 0:
            dd = (peak - v) / peak
            max_dd = max(max_dd, dd)
    return max_dd


def _monthly_returns(equity_rows: list[dict]) -> dict[str, float]:
    by_month: dict[str, list[float]] = defaultdict(list)
    for row in equity_rows:
        ts = _parse_dt(str(row.get("ts") or ""))
        if ts is None:
            continue
        by_month[ts.strftime("%Y-%m")].append(_to_float(row.get("total_usdt", 0.0)))
    out: dict[str, float] = {}
    for month, vals in by_month.items():
        if len(vals) < 2 or vals[0] <= 0:
            out[month] = 0.0
            continue
        out[month] = (vals[-1] - vals[0]) / vals[0]
    return out


def _score_bucket(score: int) -> str:
    if score >= 90:
        return "90-100"
    if score >= 80:
        return "80-89"
    if score >= 70:
        return "70-79"
    if score >= 65:
        return "65-69"
    return "<65"


def build_performance_snapshot(*, mode: str, lookback_days: int = 180) -> dict:
    """Build analytics snapshot for the requested mode and lookback window."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    closed = [r for r in storage.closed_trades(limit=5000) if r.get("mode") == mode]
    closed = [r for r in closed if (_parse_dt(str(r.get("exit_ts") or "")) or cutoff) >= cutoff]

    pnls = [_to_float(r.get("pnl", 0.0)) for r in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    trade_count = len(pnls)

    win_rate = (len(wins) / trade_count) if trade_count else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf") if gross_profit > 0 else 0.0
    avg_winner = mean(wins) if wins else 0.0
    avg_loser = mean(losses) if losses else 0.0
    expectancy = mean(pnls) if pnls else 0.0

    returns = [_to_float(r.get("pnl_pct", 0.0)) / 100.0 for r in closed]
    sharpe = 0.0
    if len(returns) > 1:
        sigma = pstdev(returns)
        if sigma > 0:
            sharpe = (mean(returns) / sigma) * math.sqrt(252.0)

    largest_winner = max(pnls) if pnls else 0.0
    largest_loser = min(pnls) if pnls else 0.0

    eq_rows = [r for r in storage.equity_curve(limit=3000) if r.get("mode") == mode]
    eq_values = [_to_float(r.get("total_usdt", 0.0)) for r in eq_rows]
    max_dd = _max_drawdown(eq_values)
    monthly = _monthly_returns(eq_rows)

    symbol_profit: dict[str, float] = defaultdict(float)
    strategy_profit: dict[str, float] = defaultdict(float)
    for row in closed:
        symbol = str(row.get("symbol") or "")
        pnl = _to_float(row.get("pnl", 0.0))
        symbol_profit[symbol] += pnl
        try:
            agents = json.loads(str(row.get("agents") or "[]"))
        except Exception:
            agents = []
        for agent in agents:
            strategy_profit[str(agent)] += pnl

    tick_rows = [r for r in storage.recent_tick_audit(limit=5000) if r.get("mode") == mode]
    score_bins: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "wins": 0})
    timeframe_bins: dict[str, dict[str, float]] = defaultdict(lambda: {"count": 0, "wins": 0})
    for row in tick_rows:
        score = int(row.get("score") or 0)
        bucket = _score_bucket(score)
        score_bins[bucket]["count"] += 1
        if int(row.get("executed") or 0) == 1:
            score_bins[bucket]["wins"] += 1

        tf = str(row.get("timeframe") or "unknown")
        timeframe_bins[tf]["count"] += 1
        if int(row.get("executed") or 0) == 1:
            timeframe_bins[tf]["wins"] += 1

    best_symbols = sorted(symbol_profit.items(), key=lambda x: x[1], reverse=True)[:5]
    worst_symbols = sorted(symbol_profit.items(), key=lambda x: x[1])[:5]
    best_strategies = sorted(strategy_profit.items(), key=lambda x: x[1], reverse=True)[:5]

    score_ranges = {
        k: {
            "count": int(v["count"]),
            "execution_rate": (v["wins"] / v["count"]) if v["count"] else 0.0,
        }
        for k, v in score_bins.items()
    }
    timeframe_quality = {
        k: {
            "count": int(v["count"]),
            "execution_rate": (v["wins"] / v["count"]) if v["count"] else 0.0,
        }
        for k, v in timeframe_bins.items()
    }

    snapshot = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "lookback_days": lookback_days,
        "trade_count": trade_count,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "average_winner": avg_winner,
        "average_loser": avg_loser,
        "sharpe_ratio": sharpe,
        "maximum_drawdown": max_dd,
        "largest_winner": largest_winner,
        "largest_loser": largest_loser,
        "monthly_returns": monthly,
        "symbol_profitability": dict(sorted(symbol_profit.items(), key=lambda x: x[0])),
        "strategy_profitability": dict(sorted(strategy_profit.items(), key=lambda x: x[0])),
        "best_symbols": best_symbols,
        "worst_symbols": worst_symbols,
        "best_strategies": best_strategies,
        "best_score_ranges": score_ranges,
        "best_timeframes": timeframe_quality,
    }
    storage.kv_set(f"performance_analytics:{mode}", snapshot)
    return snapshot


def run_and_log_snapshot(*, mode: str, lookback_days: int = 180) -> dict:
    snapshot = build_performance_snapshot(mode=mode, lookback_days=lookback_days)
    log.info(
        "analytics mode=%s trades=%d win_rate=%.2f%% pf=%.2f sharpe=%.2f max_dd=%.2f%%",
        mode,
        snapshot["trade_count"],
        snapshot["win_rate"] * 100,
        snapshot["profit_factor"] if snapshot["profit_factor"] != float("inf") else 999.0,
        snapshot["sharpe_ratio"],
        snapshot["maximum_drawdown"] * 100,
    )
    return snapshot
