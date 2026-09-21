"""Build rejected-setup outcome data (Stage 1, READ-ONLY).

Scans the existing ``tick_audit`` table for BUY setups that were rejected,
simulates the hypothetical outcome from later real daily candles using the
CURRENT live exit ladder (real fees + slippage, stop-first intrabar), and
stores the results in a SEPARATE research database — never a live table.

This script fetches public Binance.US market data (read-only) and NEVER places,
cancels, or modifies orders, touches live tables, or changes any live setting.

Usage:
    python -m scripts.simulate_rejected_setups                 # mode=live
    python -m scripts.simulate_rejected_setups --mode paper
    python -m scripts.simulate_rejected_setups --slippage 0.0015 --max-symbols 40
    python -m scripts.simulate_rejected_setups --db data/cache/research.db
"""
from __future__ import annotations

import argparse
from pathlib import Path

from app.research.rejected_setups import (
    DEFAULT_SLIPPAGE_PCT,
    build_rejected_setup_outcomes,
    default_research_db,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", default="live", choices=["live", "paper"],
                    help="Which tick_audit mode to analyze (default: live).")
    ap.add_argument("--slippage", type=float, default=DEFAULT_SLIPPAGE_PCT,
                    help=f"Per-fill slippage estimate as a fraction (default: {DEFAULT_SLIPPAGE_PCT}).")
    ap.add_argument("--db", default=str(default_research_db()),
                    help="Research SQLite file to write (default: <data_cache_dir>/research.db).")
    ap.add_argument("--max-symbols", type=int, default=None,
                    help="Cap the number of distinct symbols fetched (bounds network calls).")
    ap.add_argument("--scan-limit", type=int, default=500_000,
                    help="Max tick_audit rows to scan (most-recent-first).")
    args = ap.parse_args()

    summary = build_rejected_setup_outcomes(
        mode=args.mode,
        db_path=Path(args.db),
        slippage_pct=args.slippage,
        scan_limit=args.scan_limit,
        max_symbols=args.max_symbols,
    )

    print("Rejected-setup simulation complete (read-only).")
    print(f"  mode                : {args.mode}")
    print(f"  research db         : {args.db}")
    print(f"  tick rows scanned   : {summary.tick_rows_scanned}")
    print(f"  rejected setups     : {summary.rejected_setups}")
    print(f"  newly simulated     : {summary.simulated}")
    print(f"  stored              : {summary.stored}")
    print(f"  skipped (existing)  : {summary.skipped_existing}")
    print(f"  skipped (no data)   : {summary.skipped_no_data}")
    print(f"  symbols covered     : {len(summary.symbols)}")
    if summary.stored:
        print("\nNext: python -m scripts.rejected_setup_report")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
