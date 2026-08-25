"""Backfill corrected-PnL columns for historical closed_trades rows.

The forensic audit (2026-08-25) found the modeled Binance.US taker fee used
for every historical PnL calculation was 0.40%, while the account's REAL rate
(per `client.trade_fees()`) is 0.02% — a 20x overstatement of trading costs
baked into every `pnl`/`pnl_pct` value ever recorded.

This script does NOT touch `pnl`/`pnl_pct` on existing rows — those are the
historical record and are preserved exactly as they were computed at the
time. Instead it populates the separate `gross_pnl` / `fee_amount` /
`fee_source` / `pnl_corrected` / `pnl_pct_corrected` columns (added alongside
this script) for any row that doesn't have them yet, using the corrected fee
rate. New rows going forward already get accurate values written at close
time (see app/storage/db.py close_position/reduce_position) since the config
default itself was corrected — this script is only for backfilling the past.

Read-only against `pnl`/`pnl_pct`; only ever ADDS values to NULL columns.

    python -m scripts.backfill_corrected_pnl              # dry run (reports only)
    python -m scripts.backfill_corrected_pnl --apply       # writes the columns
    python -m scripts.backfill_corrected_pnl --apply --fee 0.0002
"""
from __future__ import annotations

import argparse
import sqlite3

from app.config import get_settings


def _conn() -> sqlite3.Connection:
    db = get_settings().data_cache_dir / "trading.db"
    if not db.exists():
        raise SystemExit(f"no DB at {db}")
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="write the backfilled columns (default: dry run)")
    p.add_argument("--fee", type=float, default=None, help="corrected taker fee fraction (default: settings.binance_taker_fee)")
    args = p.parse_args()

    corrected_fee = args.fee if args.fee is not None else get_settings().binance_taker_fee
    print(f"Using corrected fee rate: {corrected_fee:.4%} per side")

    with _conn() as c:
        rows = c.execute(
            "SELECT id, qty, entry_price, exit_price, pnl FROM closed_trades "
            "WHERE gross_pnl IS NULL OR fee_amount IS NULL OR pnl_corrected IS NULL"
        ).fetchall()
        print(f"{len(rows)} row(s) missing corrected-PnL columns")

        updates = []
        for r in rows:
            qty = float(r["qty"])
            entry = float(r["entry_price"])
            exit_p = float(r["exit_price"])
            gross_pnl = (exit_p - entry) * qty
            fee_amount = (entry * qty * corrected_fee) + (exit_p * qty * corrected_fee)
            pnl_corrected = gross_pnl - fee_amount
            entry_notional = entry * qty
            pnl_pct_corrected = (pnl_corrected / entry_notional * 100) if entry_notional else 0.0
            updates.append((gross_pnl, fee_amount, "modeled_corrected_backfill", pnl_corrected, pnl_pct_corrected, r["id"]))

        total_original = sum(float(r["pnl"]) for r in rows)
        total_corrected = sum(u[3] for u in updates)
        print(f"original recorded pnl (unchanged, preserved): {total_original:.2f}")
        print(f"corrected pnl (new field, backfilled):        {total_corrected:.2f}")
        print(f"difference (fee overstatement removed):       {total_corrected - total_original:.2f}")

        if not args.apply:
            print("\nDry run — no columns written. Re-run with --apply to persist.")
            return

        c.executemany(
            "UPDATE closed_trades SET gross_pnl=?, fee_amount=?, fee_source=?, "
            "pnl_corrected=?, pnl_pct_corrected=? WHERE id=?",
            updates,
        )
        c.commit()
        print(f"\nBackfilled {len(updates)} row(s). `pnl`/`pnl_pct` were NOT modified.")


if __name__ == "__main__":
    main()
