"""Execution quality report (Stage 4, READ-ONLY, measurement only).

Measures realized entry spread cost per symbol, the per-entry economics of a
maker (limit-at-bid) entry vs the live taker/market entry, and estimates the
missed-fill rate from real candles. Recommends only — it does NOT change the
live order type.

Usage:
    python -m scripts.execution_quality_report
    python -m scripts.execution_quality_report --mode live --max-symbols 40
    python -m scripts.execution_quality_report --no-fill-estimate   # skip network
"""
from __future__ import annotations

import argparse

from app.config import get_settings
from app.research.execution_quality import (
    breakeven_fill_rate,
    collect_buy_entries,
    entry_spread_costs,
    estimate_limit_fill_rate,
    expected_entry_benefit,
    maker_taker_economics,
)
from app.research.rejected_setups import fetch_daily_candles
from app.storage import storage
from app.trading.experiment_stats import compute_stats, load_live_trades


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", default="live", choices=["live", "paper"])
    ap.add_argument("--max-symbols", type=int, default=None,
                    help="Cap distinct symbols in the fill-rate estimate (bounds network).")
    ap.add_argument("--no-fill-estimate", action="store_true",
                    help="Skip the candle-based fill-rate estimate (fully offline).")
    ap.add_argument("--top", type=int, default=15, help="Show N worst-spread symbols.")
    args = ap.parse_args()

    s = get_settings()
    taker = float(s.binance_taker_fee)
    maker = float(getattr(s, "binance_maker_fee", taker))

    tick_rows = storage.recent_tick_audit(limit=200_000)
    costs = entry_spread_costs(tick_rows, mode=args.mode)

    print("Execution quality report (READ-ONLY — recommends only, no order-type change)")
    print(f"  mode: {args.mode}   taker_fee={taker:.3%}  maker_fee={maker:.3%}")
    if costs.overall_avg_spread_pct is None:
        print("  no executed-BUY spread data available — nothing to measure.")
        return 0
    print(f"  executed BUY entries with spread data: {costs.n_entries}")
    print(f"  overall avg entry spread: {costs.overall_avg_spread_pct:.3%}\n")

    print(f"=== WORST ENTRY SPREADS BY SYMBOL (top {args.top}) ===")
    print(f"  {'symbol':<12} {'n':>4} {'avg_spread':>11}")
    for sym in costs.by_symbol[:args.top]:
        print(f"  {sym.symbol:<12} {sym.n:>4} {sym.avg_spread_pct:>11.3%}")

    econ = maker_taker_economics(costs.overall_avg_spread_pct, taker_fee=taker, maker_fee=maker)
    print("\n=== MAKER vs TAKER ENTRY ECONOMICS (per entry) ===")
    print(f"  taker entry cost (fee + half-spread): {econ.taker_entry_cost_pct:+.3%}")
    print(f"  maker entry cost (fee - half-spread): {econ.maker_entry_cost_pct:+.3%}")
    print(f"  saving per FILLED maker entry:        {econ.saving_if_filled_pct:+.3%}")

    # Live net expectancy per trade = the edge forgone when a limit misses.
    live_trades = load_live_trades(mode=args.mode)
    forgone = compute_stats(live_trades).expectancy_pct
    print(f"\n  live net expectancy per trade (forgone if a limit misses): {forgone:+.3%}")

    if args.no_fill_estimate:
        print("\n  fill-rate estimate skipped (--no-fill-estimate).")
    else:
        entries = collect_buy_entries(tick_rows, mode=args.mode)
        if args.max_symbols is not None:
            seen: set[str] = set()
            capped = []
            for sym, ts in entries:
                if sym not in seen and len(seen) >= args.max_symbols:
                    continue
                seen.add(sym)
                capped.append((sym, ts))
            entries = capped
        offset = costs.overall_avg_spread_pct / 2.0  # post at ~the bid
        print(f"\n=== MISSED-FILL ESTIMATE (limit at bid, offset {offset:.3%}) ===")
        est = estimate_limit_fill_rate(entries, fetch_daily_candles, offset_pct=offset)
        if est.fill_rate is None:
            print("  insufficient candle data to estimate fill rate.")
        else:
            print(f"  evaluated {est.n} entries: filled {est.filled} "
                  f"({est.fill_rate:.0%}), missed {est.missed_rate:.0%}")
            print("  CAVEAT: daily candles almost always dip below the bid intrabar, so this")
            print("  fill rate is an UPPER BOUND — it understates real intraday miss risk")
            print("  (a resting limit can fill late or miss the move). Treat as optimistic.")
            benefit = expected_entry_benefit(
                saving_if_filled_pct=econ.saving_if_filled_pct,
                fill_rate=est.fill_rate,
                forgone_edge_per_miss_pct=forgone,
            )
            be = breakeven_fill_rate(
                saving_if_filled_pct=econ.saving_if_filled_pct,
                forgone_edge_per_miss_pct=forgone,
            )
            print(f"  expected per-opportunity change vs taker: {benefit:+.3%}")
            if be is None:
                print("  breakeven fill rate: n/a (forgone edge <= 0 — missing trades "
                      "does not hurt, so limit entries dominate on cost alone)")
            else:
                print(f"  breakeven fill rate: {be:.0%} "
                      f"(limit entries help while fills stay above this)")

    print("\nRECOMMENDATION (measurement only — do NOT change live order type here):")
    print("  Use the numbers above to decide whether a limit/maker entry pilot is")
    print("  worth an isolated paper A/B. If live expectancy is negative, execution")
    print("  savings alone will not make the strategy profitable.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
