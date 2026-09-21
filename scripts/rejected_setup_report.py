"""Rejected-setup report (Stage 1, READ-ONLY analysis).

Reads the research database produced by ``scripts/simulate_rejected_setups`` and
reports, grouped by reject reason: how many would-be winners each filter blocked,
how many losses it saved, with counts, win rate, expectancy, profit factor, and a
bootstrap confidence interval on expectancy. Groups with fewer than 30 samples are
flagged "INSUFFICIENT EVIDENCE".

This is analysis only. It reads no live table and changes no live gating.

Usage:
    python -m scripts.rejected_setup_report
    python -m scripts.rejected_setup_report --by regime      # split by BTC regime
    python -m scripts.rejected_setup_report --db data/cache/research.db
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from pathlib import Path

from app.research.bootstrap import bootstrap_ci, expectancy, profit_factor, win_rate
from app.research.rejected_setups import MIN_SAMPLES, ResearchStore, default_research_db


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def _print_group_table(title: str, groups: dict[str, list[dict]]) -> None:
    print(f"\n=== {title} ===")
    header = (
        f"{'group':<20} {'n':>5} {'win%':>6} {'expect%':>9} {'exp_CI95%':>18} "
        f"{'pf':>5} {'winners':>7} {'losses':>7} {'evidence':>18}"
    )
    print(header)
    print("-" * len(header))

    for name in sorted(groups, key=lambda k: -len(groups[k])):
        rows = groups[name]
        pnls = [float(r["sim_pnl_pct"]) for r in rows]
        n = len(pnls)
        winners = sum(1 for x in pnls if x > 0)
        losers = sum(1 for x in pnls if x <= 0)
        wr = win_rate(pnls)
        exp = expectancy(pnls)
        pf = profit_factor(pnls)
        lo, hi = bootstrap_ci(pnls)
        evidence = "ok" if n >= MIN_SAMPLES else "INSUFFICIENT"
        ci = f"[{lo:+.2%}, {hi:+.2%}]"
        print(
            f"{name:<20} {n:>5} {wr:>6.1%} {exp:>+9.2%} {ci:>18} "
            f"{_fmt_pf(pf):>5} {winners:>7} {losers:>7} {evidence:>18}"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(default_research_db()),
                    help="Research SQLite file to read.")
    ap.add_argument("--by", default="reason", choices=["reason", "regime", "exit", "entry"],
                    help="Grouping dimension (default: reject reason).")
    args = ap.parse_args()

    store = ResearchStore(Path(args.db))
    outcomes = store.all_outcomes()
    if not outcomes:
        print(f"No rejected-setup outcomes found in {args.db}.")
        print("Run: python -m scripts.simulate_rejected_setups")
        return 0

    all_pnls = [float(r["sim_pnl_pct"]) for r in outcomes]
    unresolved = sum(1 for r in outcomes if int(r.get("unresolved") or 0) == 1)
    lo, hi = bootstrap_ci(all_pnls)
    print("Rejected-setup outcome analysis (READ-ONLY — analysis does not change live gating)")
    print(f"  research db          : {args.db}")
    print(f"  simulated setups     : {len(outcomes)}")
    print(f"  right-censored (open): {unresolved}")
    print(f"  overall win rate     : {win_rate(all_pnls):.1%}")
    print(f"  overall expectancy   : {expectancy(all_pnls):+.2%} per setup "
          f"(bootstrap 95% CI [{lo:+.2%}, {hi:+.2%}])")
    print(f"  overall profit factor: {_fmt_pf(profit_factor(all_pnls))}")

    groups: dict[str, list[dict]] = defaultdict(list)
    if args.by == "reason":
        for r in outcomes:
            for reason in r.get("reject_reasons") or ["unknown"]:
                groups[reason].append(r)
        title = "By reject reason (a setup counts toward every filter that blocked it)"
    elif args.by == "regime":
        for r in outcomes:
            groups[str(r.get("btc_regime_label") or "unknown")].append(r)
        title = "By BTC regime label"
    elif args.by == "exit":
        for r in outcomes:
            groups[str(r.get("exit_reason") or "unknown")].append(r)
        title = "By simulated exit reason"
    else:  # entry
        for r in outcomes:
            groups[str(r.get("entry_type") or "unknown")].append(r)
        title = "By entry type"

    _print_group_table(title, groups)

    if args.by == "reason":
        print(
            "\nReading this table: a HIGH win% / positive expectancy group is a filter "
            "that is BLOCKING WINNERS (a cost); a LOW win% / negative expectancy group "
            "is a filter that is SAVING LOSSES (a benefit). Groups marked INSUFFICIENT "
            f"(< {MIN_SAMPLES} samples) are not yet conclusive — do not act on them."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
