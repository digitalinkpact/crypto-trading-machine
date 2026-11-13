"""Read-only snapshot of autopilot state, recent decisions, and live orders.

Run from the repo root: `python -m scripts.monitor_tick_events [--mode live]`.
The DB is opened read-only. Pytest fixture rows are hidden unless
`--include-test`, and internal gate decisions are never shown as Binance
rejections (see app/trading/audit_labels.py).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.trading import audit_labels


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _kv(conn: sqlite3.Connection, key: str):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def report(conn: sqlite3.Connection, *, mode: str, include_test: bool = False, limit: int = 12) -> None:
    state = _kv(conn, "autopilot_state") or {}
    last_tick = _parse_iso(state.get("last_tick_at"))
    now = datetime.now(timezone.utc)
    tick_age_s = int((now - last_tick).total_seconds()) if last_tick else None

    print("=== AUTOPILOT ===")
    print(f"running={state.get('running')} mode={state.get('mode')} last_tick_at={state.get('last_tick_at')} tick_age_s={tick_age_s}")
    print(f"last_completed_tick_at={_kv(conn, 'autopilot_last_completed_tick')}")
    print(f"last_action={state.get('last_action')} last_error={state.get('last_error')}")

    print(f"\n=== LAST TRADE DECISIONS (mode={mode}{', incl. TEST rows' if include_test else ''}) ===")
    rows = audit_labels.fetch_trade_audit(conn, mode=mode, limit=limit, include_test=include_test)
    if not rows:
        print("no trade_audit rows")
    for row in rows:
        stage = audit_labels.execution_stage(row)
        reason = audit_labels.rejection_reason(row) if stage == audit_labels.INTERNAL_REJECTED else row["final_outcome"]
        print(f"{row['ts']} {row['symbol']:<12} {row['signal']:<4} conf={row['confidence']} "
              f"[{audit_labels.STAGE_LABELS[stage]}] {reason}")

    print(f"\n=== LAST {mode.upper()} ORDERS (recorded fills) ===")
    orders = conn.execute(
        "SELECT id,ts,symbol,side,qty,price,client_order_id,agents "
        "FROM orders WHERE mode=? ORDER BY id DESC LIMIT 20", (mode,),
    ).fetchall()
    if not orders:
        print(f"no {mode} orders")
    for order in orders:
        flag = " [OPERATOR TEST ORDER]" if audit_labels.is_operator_test_order(order) else ""
        print(f"{order['ts']} {order['symbol']} {order['side']} qty={order['qty']} @ {order['price']} "
              f"coid={order['client_order_id']}{flag}")

    print("\n=== BUY->SELL DETECTION (recent orders) ===")
    by_symbol: dict[str, list[sqlite3.Row]] = {}
    for order in reversed(orders):
        by_symbol.setdefault(order["symbol"], []).append(order)
    found = False
    for symbol, seq in by_symbol.items():
        for i in range(len(seq) - 1):
            if seq[i]["side"] == "BUY" and seq[i + 1]["side"] == "SELL":
                found = True
                print(f"{symbol}: BUY at {seq[i]['ts']} -> SELL at {seq[i + 1]['ts']}")
    if not found:
        print("no immediate BUY->SELL sequence found in recent orders")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="live", choices=("live", "paper"))
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--include-test", action="store_true", help="also show pytest fixture rows")
    args = parser.parse_args()
    try:
        conn = audit_labels.connect_readonly(args.db)
    except FileNotFoundError as exc:
        print(exc)
        return
    with conn:
        report(conn, mode=args.mode, include_test=args.include_test)


if __name__ == "__main__":
    main()
