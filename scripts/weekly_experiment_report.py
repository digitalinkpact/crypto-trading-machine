"""Weekly experiment report — one page (READ-ONLY).

Combines the three read-only evidence layers into a single at-a-glance page:

  * Stage 1 — rejected-setup findings (which filters block winners / save losses)
  * Stage 2 — live statistics with confidence intervals + promotion verdict
  * Stage 3 — live vs backtest drift (strategy vs execution)

Analysis only. Reads no live setting and changes nothing — a human acts on it.

Usage:
    python -m scripts.weekly_experiment_report
    python -m scripts.weekly_experiment_report --window-days 0 --backtest-json bt.json
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
from app.research.rejected_setups import ResearchStore, default_research_db, summarize_by_reason
from app.trading.experiment_stats import Stats, build_report


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def _section_rejected(db_path: Path, top: int = 8) -> None:
    print("── STAGE 1 · REJECTED-SETUP FINDINGS " + "─" * 40)
    try:
        outcomes = ResearchStore(db_path).all_outcomes()
    except Exception as exc:  # noqa: BLE001
        print(f"  (could not read {db_path}: {exc})")
        return
    if not outcomes:
        print("  no data — run: python -m scripts.simulate_rejected_setups")
        return
    print(f"  simulated rejected setups: {len(outcomes)}")
    rows = summarize_by_reason(outcomes)
    print(f"  {'reject reason':<18} {'n':>5} {'win%':>6} {'expect%':>9} "
          f"{'exp_CI95%':>18}  evidence")
    for r in rows[:top]:
        lo, hi = r["ci"]
        ev = "ok" if r["sufficient"] else "INSUFFICIENT"
        print(f"  {r['reason']:<18} {r['n']:>5} {r['win_rate']:>6.1%} "
              f"{r['expectancy_pct']:>+9.2%} [{lo:+.2%},{hi:+.2%}]  {ev}")
    print("  (high win% = filter blocking winners; low win% = filter saving losses)")


def _section_live_stats(mode: str) -> dict:
    print("\n── STAGE 2 · LIVE STATS + PROMOTION " + "─" * 41)
    report = build_report(mode=mode)
    overall: Stats = report["overall"]
    lo, hi = overall.expectancy_pct_ci
    print(f"  n={overall.n}  win%={overall.win_rate:.1%}  "
          f"expectancy={overall.expectancy_pct:+.2%} (95% CI [{lo:+.2%},{hi:+.2%}])  "
          f"pf={_fmt_pf(overall.profit_factor)}  net=${overall.net_pnl_usd:+.2f}")
    v = report["verdict"]
    print(f"  VERDICT: {v.verdict}")
    for c in v.criteria:
        mark = "PASS" if c.passed is True else "FAIL" if c.passed is False else "N/A "
        print(f"    [{mark}] {c.name}: {c.detail}")
    return report


def _section_drift(mode: str, window_days: int, baseline) -> None:
    print("\n── STAGE 3 · LIVE vs BACKTEST DRIFT " + "─" * 41)
    live = build_live_snapshot(mode=mode, window_days=window_days)
    report = compute_drift(live, baseline)
    print(f"  live window: {live.window_label} (n={live.n_trades})  "
          f"baseline: {baseline.source}")
    for m in report.metrics:
        live_s = f"{m.live:+.2%}" if m.live is not None else "n/a"
        bt_s = f"{m.backtest:+.2%}" if m.backtest is not None else "n/a"
        flag = "MATERIAL" if m.material else "ok"
        print(f"    {m.name:<22} {m.kind:<9} live={live_s:>9} bt={bt_s:>9}  {flag}")
    print(f"  strategy drift: {'YES' if report.strategy_drift else 'no'}   "
          f"execution drift: {'YES' if report.execution_drift else 'no'}")
    for n in report.notes:
        print(f"    note: {n}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", default="live", choices=["live", "paper"])
    ap.add_argument("--window-days", type=int, default=30,
                    help="Rolling live window for the drift section (0 = all).")
    ap.add_argument("--db", default=str(default_research_db()),
                    help="Rejected-setup research DB (Stage 1 output).")
    ap.add_argument("--backtest-json", default=None,
                    help="JSON baseline for the drift section.")
    args = ap.parse_args()

    baseline = (
        load_baseline_from_json(Path(args.backtest_json)) if args.backtest_json
        else default_baseline()
    )

    print("=" * 78)
    print(f"WEEKLY EXPERIMENT REPORT  ·  mode={args.mode}  ·  READ-ONLY (changes nothing)")
    print("=" * 78)
    _section_rejected(Path(args.db))
    _section_live_stats(args.mode)
    _section_drift(args.mode, args.window_days, baseline)
    print("\n" + "=" * 78)
    print("All figures are evidence for a human to act on. No live setting, gate, "
          "sizing,\nor order type was read-write or changed by this report.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
