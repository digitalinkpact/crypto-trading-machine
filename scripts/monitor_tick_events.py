from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def main() -> None:
    db = Path("data/cache/trading.db")
    if not db.exists():
        print("DB not found at data/cache/trading.db")
        return

    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row

    state_row = conn.execute("SELECT value FROM kv WHERE key='autopilot_state'").fetchone()
    state = json.loads(state_row["value"]) if state_row else {}
    last_tick = _parse_iso(state.get("last_tick_at"))
    now = datetime.now(timezone.utc)
    tick_age_s = int((now - last_tick).total_seconds()) if last_tick else None

    print("=== AUTOPILOT ===")
    print(f"running={state.get('running')} mode={state.get('mode')} last_tick_at={state.get('last_tick_at')} tick_age_s={tick_age_s}")
    print(f"last_action={state.get('last_action')} last_error={state.get('last_error')}")

    print("\n=== LAST TRADE AUDIT (LIVE) ===")
    rows = conn.execute(
        "SELECT ts,symbol,signal,confidence,execution_attempted,binance_response,final_outcome "
        "FROM trade_audit ORDER BY id DESC LIMIT 12"
    ).fetchall()
    if not rows:
        print("no trade_audit rows")
    for r in rows:
        print(dict(r))

    print("\n=== LAST LIVE ORDERS ===")
    orders = conn.execute(
        "SELECT id,ts,symbol,side,qty,price,client_order_id "
        "FROM orders WHERE mode='live' ORDER BY id DESC LIMIT 20"
    ).fetchall()
    if not orders:
        print("no live orders")
    for r in orders:
        print(dict(r))

    print("\n=== BUY->SELL DETECTION (recent live orders) ===")
    by_symbol: dict[str, list[sqlite3.Row]] = {}
    for r in reversed(orders):
        by_symbol.setdefault(r["symbol"], []).append(r)

    found = False
    for symbol, seq in by_symbol.items():
        for i in range(len(seq) - 1):
            if seq[i]["side"] == "BUY" and seq[i + 1]["side"] == "SELL":
                found = True
                print(
                    f"{symbol}: BUY at {seq[i]['ts']} -> SELL at {seq[i + 1]['ts']}"
                )
    if not found:
        print("no immediate BUY->SELL sequence found in recent live orders")


if __name__ == "__main__":
    main()
