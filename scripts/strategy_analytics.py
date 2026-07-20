"""ProfitStream analytics report from live/paper DB history.

Outputs win rate, profit factor, sharpe, drawdown, avg hold time,
and indicator effectiveness based on tick_audit outcomes.
"""
from __future__ import annotations

from datetime import datetime
from math import sqrt
from statistics import mean, pstdev

from app.storage import storage


def _parse_dt(v: str) -> datetime:
    return datetime.fromisoformat(v)


def main() -> None:
    closed = storage.closed_trades(limit=5000)
    if not closed:
        print("No closed trades available")
        return

    wins = [t for t in closed if float(t["pnl"]) > 0]
    losses = [t for t in closed if float(t["pnl"]) < 0]
    rets = [float(t["pnl_pct"]) / 100.0 for t in closed]

    win_rate = len(wins) / len(closed)
    gross_profit = sum(float(t["pnl"]) for t in wins)
    gross_loss = abs(sum(float(t["pnl"]) for t in losses))
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
    for t in closed:
        try:
            entry = _parse_dt(str(t["entry_ts"]))
            exit_ = _parse_dt(str(t["exit_ts"]))
            hold_hours.append((exit_ - entry).total_seconds() / 3600.0)
        except ValueError:
            continue

    tick_rows = storage.recent_tick_audit(limit=5000)
    indicator_scores: dict[str, list[int]] = {
        "ema_cross_1m": [],
        "rsi_ok": [],
        "volume_spike_1m": [],
        "macd_bull_15m": [],
        "btc_aligned_1h": [],
    }
    for row in tick_rows:
        if row.get("action") != "BUY" or int(row.get("executed", 0)) != 1:
            continue
        payload = row.get("indicators") or "{}"
        if isinstance(payload, str):
            import json

            try:
                ind = json.loads(payload)
            except json.JSONDecodeError:
                ind = {}
        else:
            ind = payload
        for k in indicator_scores:
            if bool(ind.get(k)):
                indicator_scores[k].append(int(row.get("score", 0)))

    ranked = sorted(
        ((k, (mean(v) if v else 0.0), len(v)) for k, v in indicator_scores.items()),
        key=lambda x: x[1],
        reverse=True,
    )

    print("=== ProfitStream Analytics ===")
    print(f"Trades: {len(closed)}")
    print(f"Win rate: {win_rate:.2%}")
    print(f"Profit factor: {profit_factor:.2f}")
    print(f"Sharpe ratio: {sharpe:.2f}")
    print(f"Max drawdown: {max_dd:.2%}")
    print(f"Average hold time: {mean(hold_hours):.2f}h" if hold_hours else "Average hold time: n/a")
    print("Best-performing indicators (avg decision score, samples):")
    for name, score, n in ranked:
        print(f"  - {name}: score={score:.1f}, samples={n}")


if __name__ == "__main__":
    main()
