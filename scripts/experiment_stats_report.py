"""Live-trade statistics + promotion verdict (Stage 2, READ-ONLY).

Reads live ``closed_trades`` and prints performance statistics (with bootstrap
confidence intervals) split by BTC regime and exit reason, then a
PROMOTE / KEEP TESTING / REJECT verdict against the pre-committed criteria in
``docs/research/promotion_criteria.md``.

Analysis only — reads no live setting and changes nothing. A human applies any
change.

Usage:
    python -m scripts.experiment_stats_report
    python -m scripts.experiment_stats_report --mode paper
"""
from __future__ import annotations

import argparse

from app.trading.experiment_stats import Stats, build_report


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def _print_stats_line(label: str, s: Stats) -> None:
    lo, hi = s.expectancy_pct_ci
    print(
        f"{label:<18} n={s.n:<4} win%={s.win_rate:>6.1%} "
        f"exp={s.expectancy_pct:>+7.2%} CI[{lo:+.2%},{hi:+.2%}] "
        f"pf={_fmt_pf(s.profit_factor):>5} "
        f"avgW=${s.avg_win_usd:>+6.2f} avgL=${s.avg_loss_usd:>+6.2f} "
        f"net=${s.net_pnl_usd:>+8.2f} maxDD=${s.max_drawdown_usd:>7.2f} "
        f"loseStreak={s.max_losing_streak:<3} mfeCap={s.mfe_captured:>5.1%}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", default="live", choices=["live", "paper"])
    args = ap.parse_args()

    report = build_report(mode=args.mode)
    overall: Stats = report["overall"]

    print("Live-trade statistics + promotion verdict (READ-ONLY — changes nothing)")
    print(f"  mode: {args.mode}\n")

    print("=== OVERALL ===")
    _print_stats_line("overall", overall)

    print("\n=== BY BTC REGIME ===")
    for label, s in sorted(report["by_regime"].items(), key=lambda kv: -kv[1].n):
        _print_stats_line(label, s)

    print("\n=== BY EXIT REASON ===")
    for label, s in sorted(report["by_exit_reason"].items(), key=lambda kv: -kv[1].n):
        _print_stats_line(label, s)

    verdict = report["verdict"]
    print(f"\n=== PROMOTION VERDICT: {verdict.verdict} ===")
    for c in verdict.criteria:
        mark = "PASS" if c.passed is True else "FAIL" if c.passed is False else "N/A "
        print(f"  [{mark}] {c.name}: {c.detail}")
    print(
        "\nThis verdict is advisory. It does not change live config, sizing, or "
        "gating — a human decides whether to act."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
