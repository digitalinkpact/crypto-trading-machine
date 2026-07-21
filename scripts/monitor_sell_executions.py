#!/usr/bin/env python3
"""Monitor and report recent SELL execution state from the local trading DB."""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
DB_PATH = REPO_ROOT / "data" / "cache" / "trading.db"

from app.exchange import BinanceUSClient


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def check_sells() -> int:
    if not DB_PATH.exists():
        print(f"❌ trading DB not found: {DB_PATH}")
        return 1

    with _conn() as c:
        latest = c.execute(
            """
            SELECT ts, mode, symbol, side, qty, price
            FROM orders
            WHERE side='SELL'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

        if latest:
            print(
                "✅ Latest SELL: "
                f"[{latest['mode']}] {latest['symbol']} {latest['qty']} @ {latest['price']} "
                f"at {latest['ts']}"
            )
        else:
            print("❌ No SELL orders found in database")

        pending = c.execute(
            """
            SELECT ts, mode, symbol, signal, confidence, final_outcome
            FROM trade_audit
            WHERE signal='SELL'
              AND final_outcome NOT IN ('executed', 'executed_sell')
            ORDER BY id DESC
            LIMIT 5
            """
        ).fetchall()

        if pending:
            print("\n⏳ Pending SELL attempts (not executed):")
            for p in pending:
                conf = p["confidence"] if p["confidence"] is not None else "-"
                print(
                    "  - "
                    f"[{p['mode']}] {p['symbol']} conf={conf} "
                    f"reason={p['final_outcome']} at {p['ts']}"
                )
        else:
            print("\n✅ No pending SELL rejections in recent audit window")

        recent = c.execute(
            """
            SELECT ts, symbol, reason, executed
            FROM tick_audit
            WHERE action='SELL'
            ORDER BY id DESC
            LIMIT 5
            """
        ).fetchall()
        if recent:
            print("\n📊 Recent SELL tick events:")
            for r in recent:
                print(
                    "  - "
                    f"{r['symbol']} executed={int(r['executed'])} reason={r['reason']} at {r['ts']}"
                )

    return 0


def _trx_live_position(c: sqlite3.Connection) -> sqlite3.Row | None:
    return c.execute(
        """
        SELECT symbol, qty, entry_price, entry_ts
        FROM positions
        WHERE mode='live' AND symbol='TRXUSDT'
        LIMIT 1
        """
    ).fetchone()


def _trx_recent_sell(c: sqlite3.Connection, since: datetime) -> sqlite3.Row | None:
    return c.execute(
        """
        SELECT ts, symbol, side, qty, price
        FROM orders
        WHERE mode='live'
          AND symbol='TRXUSDT'
          AND side='SELL'
          AND ts >= ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (since.isoformat(),),
    ).fetchone()


def _trx_latest_trade_audit_sell(c: sqlite3.Connection) -> sqlite3.Row | None:
        return c.execute(
                """
                SELECT id, ts, symbol, signal, confidence, final_outcome, detail
                FROM trade_audit
                WHERE mode='live'
                    AND symbol='TRXUSDT'
                    AND signal='SELL'
                ORDER BY id DESC
                LIMIT 1
                """
        ).fetchone()


def _trx_latest_tick_audit_sell(c: sqlite3.Connection) -> sqlite3.Row | None:
        return c.execute(
                """
                SELECT id, ts, symbol, action, executed, reason
                FROM tick_audit
                WHERE mode='live'
                    AND symbol='TRXUSDT'
                    AND action='SELL'
                ORDER BY id DESC
                LIMIT 1
                """
        ).fetchone()


def _fmt_ts(ts: str | None) -> str:
    return ts or "-"


def wait_for_trx_sell(timeout_minutes: int = 60, poll_seconds: int = 30) -> int:
    """Poll the local DB until a TRXUSDT live SELL appears or timeout expires.

    Set timeout_minutes <= 0 to run indefinitely.
    """
    if not DB_PATH.exists():
        print(f"❌ trading DB not found: {DB_PATH}")
        return 1

    if poll_seconds < 1:
        poll_seconds = 1

    start = datetime.now(timezone.utc)
    deadline = None if timeout_minutes <= 0 else (start + timedelta(minutes=timeout_minutes))
    timeout_label = "infinite" if deadline is None else f"{timeout_minutes}m"
    print(f"🔍 Monitoring for TRXUSDT SELL (timeout: {timeout_label}, poll: {poll_seconds}s)")
    print(f"🕒 Started at {start.isoformat()}")

    client = BinanceUSClient()
    check_count = 0
    last_trade_audit_id = 0
    last_tick_audit_id = 0

    with _conn() as init_conn:
        row = _trx_latest_trade_audit_sell(init_conn)
        if row is not None:
            last_trade_audit_id = int(row["id"])
        row = _trx_latest_tick_audit_sell(init_conn)
        if row is not None:
            last_tick_audit_id = int(row["id"])

    with _conn() as c:
        while True:
            check_count += 1
            sell = _trx_recent_sell(c, start - timedelta(seconds=60))
            if sell is not None:
                print(
                    "✅ TRXUSDT SELL EXECUTED: "
                    f"qty={sell['qty']} @ {sell['price']} at {_fmt_ts(sell['ts'])}"
                )
                return 0

            trade_audit_sell = _trx_latest_trade_audit_sell(c)
            if trade_audit_sell is not None and int(trade_audit_sell["id"]) > last_trade_audit_id:
                last_trade_audit_id = int(trade_audit_sell["id"])
                conf = trade_audit_sell["confidence"]
                conf_txt = "-" if conf is None else f"{float(conf):.3f}"
                print(
                    "⚠️ [TRX_SELL_INTENT] "
                    f"ts={trade_audit_sell['ts']} conf={conf_txt} "
                    f"outcome={trade_audit_sell['final_outcome']}"
                )

            tick_audit_sell = _trx_latest_tick_audit_sell(c)
            if tick_audit_sell is not None and int(tick_audit_sell["id"]) > last_tick_audit_id:
                last_tick_audit_id = int(tick_audit_sell["id"])
                print(
                    "⚠️ [TRX_TICK_SELL] "
                    f"ts={tick_audit_sell['ts']} executed={int(tick_audit_sell['executed'])} "
                    f"reason={tick_audit_sell['reason']}"
                )

            pos = _trx_live_position(c)
            if pos is None:
                print("ℹ️ No live TRXUSDT position currently open")
            else:
                entry = Decimal(str(pos["entry_price"]))
                qty = Decimal(str(pos["qty"]))
                try:
                    current = Decimal(str(asyncio.run(client.ticker_price("TRXUSDT"))))
                except Exception as exc:  # noqa: BLE001
                    print(f"⚠️ TRX price fetch failed: {exc}")
                    current = entry
                pnl_pct = ((current - entry) / entry * Decimal("100")) if entry > 0 else Decimal("0")
                target_price = entry * Decimal("1.02")
                print(
                    "[TRX_STATUS] "
                    f"qty={qty} entry={entry} price={current} pnl={pnl_pct:.2f}% "
                    f"target_2pct={target_price:.6f}"
                )

            if check_count % 10 == 0:
                print(f"[{datetime.now(timezone.utc).isoformat()}] still watching... check #{check_count}")

            if deadline is not None:
                remaining = int((deadline - datetime.now(timezone.utc)).total_seconds())
                if remaining <= 0:
                    break
                time.sleep(max(1, min(poll_seconds, remaining)))
            else:
                time.sleep(poll_seconds)

    print(f"❌ Timeout: No TRXUSDT SELL executed within {timeout_minutes} minutes")
    return 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitor SELL execution state")
    parser.add_argument("--wait-trx", action="store_true", help="Wait for TRXUSDT SELL execution")
    parser.add_argument(
        "--timeout-minutes",
        type=int,
        default=60,
        help="Timeout for wait mode (<=0 runs continuously until SELL)",
    )
    parser.add_argument("--poll-seconds", type=int, default=30, help="Polling interval for wait mode")
    args = parser.parse_args()

    if args.wait_trx:
        raise SystemExit(wait_for_trx_sell(args.timeout_minutes, args.poll_seconds))
    raise SystemExit(check_sells())
