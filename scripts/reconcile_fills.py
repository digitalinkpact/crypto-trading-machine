#!/usr/bin/env python3
"""Verify live fills against Binance.US order and trade records.

    python -m scripts.reconcile_fills            # verify, report, no halt
    python -m scripts.reconcile_fills --halt     # also block new entries on a discrepancy
    python -m scripts.reconcile_fills --report   # show stored results only (no exchange calls)

Exchange access is read-only (order query + myTrades); nothing is placed,
cancelled, or amended. Results go to the `fill_reconciliation` table.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import timedelta

from app.trading import audit_labels


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--halt", action="store_true", help="engage new_entries_blocked on discrepancy")
    parser.add_argument("--days", type=int, default=7, help="lookback for orders without intents")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--report", action="store_true", help="print stored results without querying")
    args = parser.parse_args()

    if not args.report:
        from app.trading.fill_reconcile import reconcile_fills

        summary = asyncio.run(reconcile_fills(
            "live", halt_on_discrepancy=args.halt,
            orders_lookback=timedelta(days=args.days), limit=args.limit,
        ))
        print(json.dumps(summary, indent=2, default=str))
    # The report path is read-only so it is safe before the schema migration has run.
    with audit_labels.connect_readonly() as conn:
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='fill_reconciliation'"
        ).fetchone()
        rows = conn.execute(
            "SELECT * FROM fill_reconciliation WHERE mode='live' ORDER BY checked_at DESC LIMIT ?",
            (args.limit,),
        ).fetchall() if has_table else []
    if not has_table:
        print("fill_reconciliation table not present yet (created on first start of this release)")
    for row in rows:
        print(f"{row['checked_at']} {row['symbol']:<12} {row['side'] or '?':4} {row['stage']:<26} "
              f"exch={row['exchange_status'] or '-'} executed={row['executed_qty'] or '-'} "
              f"trades={row['trade_qty'] or '-'} local={row['local_qty'] or '-'} "
              f"{('DISCREPANCY: ' + row['discrepancy']) if row['discrepancy'] else ''}")


if __name__ == "__main__":
    main()
