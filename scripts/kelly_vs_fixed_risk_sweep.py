"""Fractional-Kelly vs fixed-risk position sizing comparison (Fix 8).

The forensic audit found `app/sizing/kelly.py`'s Kelly formula is dead code —
live sizing actually uses a fixed 1%-risk-per-trade model
(`risk_per_trade_pct` / `stop_loss_pct`), capped by `kelly_fraction_cap` and
`max_position_pct` (see app/trading/risk_manager.py). This script computes
win-rate/payoff-ratio from REAL closed trades, derives full and fractional
Kelly fractions from them, and prints a side-by-side comparison against the
currently-live fixed-risk sizing — for comparison only. It does NOT change,
call, or wire anything into live position sizing. Per explicit instruction:
do NOT activate full Kelly; fractional Kelly is evaluated only as a capped,
conservative alternative to compare against the status quo.

Read-only. Point at an alternate/copied DB via DATA_CACHE_DIR, e.g.:

    DATA_CACHE_DIR=/tmp/droplet_db python -m scripts.kelly_vs_fixed_risk_sweep --mode live
"""
from __future__ import annotations

import argparse
import sqlite3

from app.config import get_settings
from app.sizing.kelly import kelly_fraction


def _conn() -> sqlite3.Connection:
    db = get_settings().data_cache_dir / "trading.db"
    if not db.exists():
        raise SystemExit(f"no DB at {db}")
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _load_pnls(mode: str) -> list[float]:
    with _conn() as c:
        rows = c.execute("SELECT pnl FROM closed_trades WHERE mode=?", (mode,)).fetchall()
    return [float(r["pnl"]) for r in rows]


def win_stats(pnls: list[float]) -> dict:
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n = len(pnls)
    win_prob = (len(wins) / n) if n else 0.0
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (abs(sum(losses)) / len(losses)) if losses else 0.0
    payoff_ratio = (avg_win / avg_loss) if avg_loss > 0 else 0.0
    return {
        "n": n, "win_prob": win_prob, "avg_win": avg_win,
        "avg_loss": avg_loss, "payoff_ratio": payoff_ratio,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", default="live", choices=["live", "paper"])
    p.add_argument("--equity", type=float, default=400.0)
    args = p.parse_args()

    pnls = _load_pnls(args.mode)
    if not pnls:
        raise SystemExit(f"no closed trades found for mode={args.mode}")
    stats = win_stats(pnls)
    s = get_settings()

    print(f"Kelly vs fixed-risk sizing comparison — mode={args.mode} n={stats['n']} trades")
    print(f"  win_prob={stats['win_prob']:.1%}  avg_win={stats['avg_win']:.4f}  "
          f"avg_loss={stats['avg_loss']:.4f}  payoff_ratio={stats['payoff_ratio']:.2f}")
    print()

    f_full = kelly_fraction(stats["win_prob"], stats["payoff_ratio"])
    print(f"Full Kelly fraction from this sample: {f_full:.1%}  "
          f"(NOT used live — small-sample Kelly is notoriously unstable and "
          f"this script does not recommend or activate it)")
    print()

    fixed_risk_notional_pct = float(s.risk_per_trade_pct) / float(s.stop_loss_pct) if s.stop_loss_pct else 0.0
    current_cap_pct = min(fixed_risk_notional_pct, s.kelly_fraction_cap, s.max_position_pct)
    print(f"Current LIVE sizing (fixed risk-per-trade, unaffected by this script):")
    print(f"  risk_per_trade_pct={s.risk_per_trade_pct:.2%} / stop_loss_pct={s.stop_loss_pct:.2%} "
          f"-> notional={fixed_risk_notional_pct:.1%} of equity, capped to "
          f"min(kelly_fraction_cap={s.kelly_fraction_cap:.0%}, max_position_pct={s.max_position_pct:.0%}) "
          f"= {current_cap_pct:.1%} of equity (${current_cap_pct * args.equity:.2f} on ${args.equity:.0f})")
    print()

    header = f"{'kelly_multiple':>14} {'raw_fraction':>13} {'capped_fraction':>16} {'notional_$':>12}"
    print(header)
    print("-" * len(header))
    for mult, label in ((1.0, "full"), (0.5, "half"), (0.25, "quarter")):
        raw = f_full * mult
        capped = min(raw, s.kelly_fraction_cap, s.max_position_pct)
        print(f"{label:>14} {raw:>12.1%} {capped:>15.1%} {capped * args.equity:>11.2f}")

    print()
    print(
        "RECOMMENDATION: keep the current fixed 1%-risk-per-trade sizing. The sample size here "
        "is small and skewed toward a losing period for the OLD strategy, so any Kelly estimate "
        "derived from it is unreliable — exactly the failure mode Kelly sizing is known for on "
        "thin samples. Do not switch to fractional Kelly until win_prob/payoff_ratio are measured "
        "cleanly under the NEW (regime-gated, exit-fixed) strategy over a larger sample."
    )


if __name__ == "__main__":
    main()
