"""ProfitStream analytics report from live/paper DB history.

Outputs position-level AND row-level win rate (labelled), profit factor, sharpe,
drawdown and average hold time. Win rate, profit factor and hold time are
measured per closed POSITION (legs grouped by symbol+entry_ts, fee-adjusted) —
see app/trading/win_rate.py.
"""
from __future__ import annotations

from datetime import datetime
from math import sqrt
from statistics import mean, pstdev

from app.storage import storage
from app.trading.win_rate import (
    collapse_to_positions,
    format_win_rate_block,
    open_position_keys,
    win_rate_summary,
)


def _parse_dt(v: str) -> datetime:
    return datetime.fromisoformat(v)


def main() -> None:
    rows = storage.closed_trades(limit=5000)
    if not rows:
        print("No closed trades available")
        return

    open_keys = open_position_keys(storage.all_positions())
    positions = collapse_to_positions(rows, open_keys)
    if not positions:
        print("No closed positions available")
        return

    wins = [p for p in positions if float(p["pnl"]) > 0]
    losses = [p for p in positions if float(p["pnl"]) < 0]
    rets = [float(p["pnl_pct"]) / 100.0 for p in positions]

    gross_profit = sum(float(p["pnl"]) for p in wins)
    gross_loss = abs(sum(float(p["pnl"]) for p in losses))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    mu = mean(rets) if rets else 0.0
    sigma = pstdev(rets) if len(rets) > 1 else 0.0
    sharpe = (mu / sigma * sqrt(252)) if sigma > 0 else 0.0

    eq = storage.equity_curve(limit=5000)
    peak = 0.0
    max_dd = 0.0
    for row in eq:
        total = float(row["total_usdt"])
        peak = max(peak, total)
        if peak > 0:
            dd = (peak - total) / peak
            max_dd = max(max_dd, dd)

    hold_hours = []
    for p in positions:
        try:
            hold_hours.append(
                (_parse_dt(str(p["exit_ts"])) - _parse_dt(str(p["entry_ts"]))).total_seconds() / 3600.0
            )
        except (ValueError, KeyError):
            continue

    print("=== ProfitStream Analytics ===")
    for line in format_win_rate_block(win_rate_summary(rows, open_keys)):
        print(line)
    print(f"Closed positions: {len(positions)}")
    print(f"Profit factor (position-level): {profit_factor:.2f}")
    print(f"Sharpe ratio (per position): {sharpe:.2f}")
    print(f"Max drawdown: {max_dd:.2%}")
    print(f"Average hold time: {mean(hold_hours):.2f}h" if hold_hours else "Average hold time: n/a")


if __name__ == "__main__":
    main()
