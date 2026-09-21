"""Weekly experiment verdict card (Stage 5, READ-ONLY).

Merges the outputs of Stages 1-4 into a single advisory verdict. Prints a
human-readable card and, with ``--json``, an audit-safe JSON dump. Applies
nothing.

Usage:
    python -m scripts.weekly_verdict                   # human readable
    python -m scripts.weekly_verdict --json            # machine readable
    python -m scripts.weekly_verdict --window-days 14  # last 14 days only
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from app.research.execution_quality import (
    SpreadCosts,
    entry_spread_costs,
    expected_entry_benefit,
    maker_taker_economics,
)
from app.research.experiment_verdict import unify_verdict
from app.research.rejected_setups import ResearchStore, default_research_db, summarize_by_reason
from app.trading.experiment_stats import evaluate_promotion, load_live_trades
from app.storage import storage as live_storage


def _try_stage1() -> Optional[list[dict]]:
    try:
        store = ResearchStore(default_research_db())
        outcomes = store.list_outcomes()
        if not outcomes:
            return None
        return summarize_by_reason(outcomes)
    except Exception:
        return None


def _stage3_drift():
    try:
        from app.research.drift import (
            avg_entry_spread_pct, build_live_snapshot, compute_drift, default_baseline,
        )
        trades = load_live_trades()
        if not trades:
            return None
        ticks = live_storage.list_tick_audit(limit=100_000)
        snap = build_live_snapshot(trades, ticks)
        return compute_drift(snap, default_baseline())
    except Exception:
        return None


def _stage4_spread_and_benefit() -> tuple[Optional[SpreadCosts], Optional[float]]:
    try:
        from app.config import get_settings
        s = get_settings()
        ticks = live_storage.list_tick_audit(limit=100_000)
        spread = entry_spread_costs(ticks)
        if spread.overall_avg_spread_pct is None:
            return spread, None
        econ = maker_taker_economics(
            spread.overall_avg_spread_pct,
            taker_fee=s.binance_taker_fee,
            maker_fee=s.binance_maker_fee,
        )
        # Approximate fill rate = 0.5 as a placeholder when no candles fetcher is
        # supplied; the real CLI (scripts/execution_quality_report.py) resolves
        # this. Kept simple here to avoid pulling data on every verdict run.
        benefit = expected_entry_benefit(
            saving_if_filled_pct=econ.saving_if_filled_pct,
            fill_rate=0.5,
            forgone_edge_per_miss_pct=0.0,
        )
        return spread, benefit
    except Exception:
        return None, None


def _print_card(card) -> None:
    print("=" * 72)
    print(f"  WEEKLY EXPERIMENT VERDICT — {card.overall}")
    print("=" * 72)
    if card.reasons:
        print("\nReasons:")
        for r in card.reasons:
            print(f"  • {r}")
    if card.concerns:
        print("\nActive concerns:")
        for c in card.concerns:
            print(f"  • {c}")
    if card.recommendations:
        print("\nRecommended actions (human decides):")
        for i, r in enumerate(card.recommendations, 1):
            print(f"  {i}. {r}")
    missing = [k for k, v in card.inputs_present.items() if not v]
    if missing:
        print(f"\nMissing inputs: {', '.join(missing)}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit JSON only")
    args = parser.parse_args()

    stage1 = _try_stage1()

    trades = load_live_trades()
    stage2 = evaluate_promotion(trades) if trades else None

    stage3 = _stage3_drift()
    stage4_spread, stage4_benefit = _stage4_spread_and_benefit()

    card = unify_verdict(
        stage1_summary=stage1,
        stage2_verdict=stage2,
        stage3_drift=stage3,
        stage4_spread=stage4_spread,
        stage4_maker_benefit_pct=stage4_benefit,
    )

    if args.json:
        print(json.dumps(card.to_dict(), indent=2, default=str))
    else:
        _print_card(card)
    return 0


if __name__ == "__main__":
    sys.exit(main())
