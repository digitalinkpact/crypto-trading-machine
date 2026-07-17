"""One-shot debug dump: trades, win rates, recent skip reasons, signal stats.

Run from repo root:

    python -m scripts.diagnose
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.config import get_settings


def _conn() -> sqlite3.Connection:
    db = get_settings().data_cache_dir / "trading.db"
    if not db.exists():
        raise SystemExit(f"no DB at {db}")
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _hdr(title: str) -> None:
    print()
    print("=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    s = get_settings()
    _hdr("Config snapshot")
    print(f"paper_trading            = {s.paper_trading}")
    print(f"min_signal_confidence    = {s.min_signal_confidence}")
    print(f"max_open_positions       = {s.max_open_positions}")
    print(f"max_long_exposure_pct    = {s.max_long_exposure_pct}")
    print(f"max_position_pct         = {s.max_position_pct}")
    print(f"stop_loss_pct            = {s.stop_loss_pct}")
    print(f"take_profit_pct          = {s.take_profit_pct}")
    print(f"trailing_stop_pct        = {s.trailing_stop_pct}")
    print(f"buy_cooldown_minutes     = {s.buy_cooldown_minutes}")
    print(f"adaptive_agent_weights   = {s.adaptive_agent_weights}")
    print(f"binance_taker_fee        = {s.binance_taker_fee} (round-trip ~{s.binance_taker_fee*2:.4f})")

    c = _conn()

    _hdr("Closed trades summary")
    rows = c.execute(
        "SELECT mode, COUNT(*) n, "
        "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins, "
        "SUM(CASE WHEN pnl<=0 THEN 1 ELSE 0 END) losses, "
        "ROUND(SUM(pnl),2) total_pnl, "
        "ROUND(AVG(pnl_pct),3) avg_pct, "
        "ROUND(MIN(pnl_pct),2) worst, ROUND(MAX(pnl_pct),2) best "
        "FROM closed_trades GROUP BY mode"
    ).fetchall()
    if not rows:
        print("(no closed trades)")
    for r in rows:
        wr = (r["wins"] / r["n"] * 100) if r["n"] else 0
        print(f"{r['mode']:6s} n={r['n']:4d} wins={r['wins']:4d} "
              f"losses={r['losses']:4d} win_rate={wr:5.1f}% "
              f"total_pnl=${r['total_pnl']:.2f} "
              f"avg={r['avg_pct']}% worst={r['worst']}% best={r['best']}%")

    _hdr("Exit reasons (last 200 closed)")
    rows = c.execute(
        "SELECT agents, pnl, pnl_pct FROM closed_trades ORDER BY id DESC LIMIT 200"
    ).fetchall()
    buckets: dict[str, list[float]] = {}
    for r in rows:
        try:
            agents = json.loads(r["agents"] or "[]")
        except Exception as e:
            import logging
            logger = logging.getLogger(__name__)
            logger.exception(f"Trade execution failure: {e}")
            raise
        reason = next((a.split(":", 1)[1] for a in agents if a.startswith("risk:")), "signal")
        buckets.setdefault(reason, []).append(float(r["pnl_pct"]))
    if not buckets:
        print("(none)")
    for reason, pcts in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        wins = sum(1 for p in pcts if p > 0)
        avg = sum(pcts) / len(pcts)
        print(f"  {reason:18s} n={len(pcts):4d} wins={wins:4d} "
              f"win_rate={wins/len(pcts)*100:5.1f}% avg={avg:+.2f}%")

    _hdr("Per-agent stats")
    rows = c.execute(
        "SELECT agent, wins, losses, total_pnl FROM agent_stats ORDER BY total_pnl DESC"
    ).fetchall()
    if not rows:
        print("(none)")
    for r in rows:
        n = r["wins"] + r["losses"]
        wr = (r["wins"] / n * 100) if n else 0
        print(f"  {r['agent']:18s} trades={n:4d} wins={r['wins']:4d} "
              f"losses={r['losses']:4d} win_rate={wr:5.1f}% "
              f"pnl=${r['total_pnl']:.2f}")

    _hdr("Open positions")
    rows = c.execute("SELECT * FROM positions").fetchall()
    if not rows:
        print("(none)")
    for r in rows:
        print(f"  {r['symbol']:10s} qty={r['qty']:.6f} entry=${r['entry_price']:.4f} "
              f"since={r['entry_ts']} mode={r['mode']}")

    _hdr("Worst symbols (avg pnl_pct, min 2 trades)")
    rows = c.execute(
        "SELECT symbol, COUNT(*) n, ROUND(AVG(pnl_pct),2) avg_pct, "
        "ROUND(SUM(pnl),2) total FROM closed_trades GROUP BY symbol "
        "HAVING n>=2 ORDER BY avg_pct ASC LIMIT 10"
    ).fetchall()
    for r in rows:
        print(f"  {r['symbol']:10s} n={r['n']:3d} avg={r['avg_pct']:+.2f}% total=${r['total']:.2f}")

    _hdr("Recent autopilot skip reasons (from kv:autopilot_skip_stats)")
    row = c.execute("SELECT value FROM kv WHERE key='autopilot_skip_stats'").fetchone()
    if not row:
        print("(none — restart the app to enable skip-reason tracking)")
    else:
        try:
            data = json.loads(row["value"])
            for k, v in sorted(data.items(), key=lambda kv: -kv[1]):
                print(f"  {k:28s} {v}")
        except Exception as exc:
            print(f"(unparseable: {exc})")

    _hdr("Recent ml_signal_events (last 10)")
    rows = c.execute(
        "SELECT ts, symbol, timeframe, action, ROUND(confidence,2) c, "
        "resolved, outcome_win, ROUND(outcome_return_pct,2) ret "
        "FROM ml_signal_events ORDER BY id DESC LIMIT 10"
    ).fetchall()
    if not rows:
        print("(none)")
    for r in rows:
        print(f"  {r['ts'][:19]} {r['symbol']:10s} {r['timeframe']:3s} "
              f"{r['action']:4s} conf={r['c']} resolved={r['resolved']} "
              f"win={r['outcome_win']} ret={r['ret']}%")


if __name__ == "__main__":
    main()
