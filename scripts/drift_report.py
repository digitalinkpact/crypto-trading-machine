"""Live vs backtest drift monitor (Stage 3, READ-ONLY).

Compares live trading against the walk-forward backtest expectation and flags
material drift, separating strategy drift (expectancy / win rate / exit-reason
mix / drawdown) from execution drift (spread at entry vs the modeled slippage).
All figures come with sample size, and expectancy carries a bootstrap CI.

Analysis only — reads no live setting and changes nothing.

Usage:
    python -m scripts.drift_report                        # default baseline, 30d window
    python -m scripts.drift_report --window-days 0        # all live history
    python -m scripts.drift_report --backtest-json bt.json
"""
from __future__ import annotations

import argparse
from pathlib import Path

from app.research.drift import (
    build_live_snapshot,
    compute_drift,
    default_baseline,
    load_baseline_from_json,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", default="live", choices=["live", "paper"])
    ap.add_argument("--window-days", type=int, default=30,
                    help="Rolling live window in days (0 = all history).")
    ap.add_argument("--backtest-json", default=None,
                    help="Path to a JSON baseline from the walk-forward run.")
    args = ap.parse_args()

    baseline = (
        load_baseline_from_json(Path(args.backtest_json)) if args.backtest_json
        else default_baseline()
    )

    live = build_live_snapshot(mode=args.mode, window_days=args.window_days)
    report = compute_drift(live, baseline)

    print("Live vs backtest drift monitor (READ-ONLY — changes nothing)")
    print(f"  mode           : {args.mode}")
    print(f"  live window    : {live.window_label} (n={live.n_trades} closed trades)")
    print(f"  baseline source: {baseline.source}\n")

    header = f"{'metric':<22} {'kind':<9} {'live':>10} {'backtest':>10} {'delta':>10}  drift"
    print(header)
    print("-" * len(header))
    for m in report.metrics:
        live_s = f"{m.live:+.2%}" if m.live is not None else "n/a"
        bt_s = f"{m.backtest:+.2%}" if m.backtest is not None else "n/a"
        d_s = f"{m.delta:+.2%}" if m.delta is not None else "n/a"
        flag = "MATERIAL" if m.material else "ok"
        print(f"{m.name:<22} {m.kind:<9} {live_s:>10} {bt_s:>10} {d_s:>10}  {flag}")

    print("\n=== VERDICT ===")
    print(f"  strategy drift : {'YES' if report.strategy_drift else 'no'}")
    print(f"  execution drift: {'YES' if report.execution_drift else 'no'}")
    for n in report.notes:
        print(f"  note: {n}")
    print("\nAnalysis only — no live setting, gate, or order type was changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
