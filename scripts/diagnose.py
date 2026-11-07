"""One-shot debug dump: position- and row-level win rates, recent skip reasons, signal stats.

Run from repo root:

    python -m scripts.diagnose
"""
from __future__ import annotations

import json
import sqlite3

from app.config import get_settings
from app.trading.win_rate import (
    REBASELINE_NOTE,
    collapse_to_positions,
    format_win_rate_block,
    open_position_keys,
    win_rate_summary,
)


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
    print(f"max_open_positions       = {s.max_open_positions} "
          f"(aggressive={s.aggressive_max_open_positions} rollback={s.rollback_max_open_positions})")
    print(f"universe_size/final_size = {s.universe_size} / {s.final_pairlist_size}")
    print(f"profitstream_score_thr   = {s.profitstream_score_threshold}")
    print(f"mean_reversion_exit      = {s.mean_reversion_exit_enabled}")
    print(f"win_rate_position_level  = {s.win_rate_position_level}")
    print(f"max_long_exposure_pct    = {s.max_long_exposure_pct}")
    print(f"max_position_pct         = {s.max_position_pct}")
    print(f"stop_loss_pct            = {s.stop_loss_pct}")
    print(f"take_profit_pct          = {s.take_profit_pct}")
    print(f"trailing_stop_pct        = {s.trailing_stop_pct}")
    print(f"buy_cooldown_minutes     = {s.buy_cooldown_minutes}")
    print(f"adaptive_agent_weights   = {s.adaptive_agent_weights}")
    print(f"binance_taker_fee        = {s.binance_taker_fee} (round-trip ~{s.binance_taker_fee*2:.4f})")

    c = _conn()

    _hdr("Win rate — position-level (target) vs row-level (legacy), by mode")
    all_rows = [dict(r) for r in c.execute("SELECT * FROM closed_trades ORDER BY id ASC").fetchall()]
    open_keys = open_position_keys(
        dict(r) for r in c.execute("SELECT symbol, mode, entry_ts FROM positions").fetchall()
    )
    if not all_rows:
        print("(no closed trades)")
    for mode in sorted({r["mode"] for r in all_rows}):
        rows = [r for r in all_rows if r["mode"] == mode]
        for line in format_win_rate_block(win_rate_summary(rows, open_keys), label=mode):
            print(line)
        positions = collapse_to_positions(rows, open_keys)
        pnl_pcts = [p["pnl_pct"] for p in positions]
        if positions:
            print(
                f"  fee-adjusted position pnl=${sum(p['pnl'] for p in positions):.2f} "
                f"avg={sum(pnl_pcts) / len(pnl_pcts):+.2f}% "
                f"worst={min(pnl_pcts):+.2f}% best={max(pnl_pcts):+.2f}%"
            )

    _hdr("Final exit reason per closed position (last 200 positions, position-level win%)")
    buckets: dict[str, list[float]] = {}
    for p in collapse_to_positions(all_rows, open_keys)[-200:]:
        buckets.setdefault(p.get("exit_reason") or "unknown", []).append(float(p["pnl"]))
    if not buckets:
        print("(none)")
    for reason, pnls in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        wins = sum(1 for x in pnls if x > 0)
        print(f"  {reason:22s} n={len(pnls):4d} wins={wins:4d} "
              f"win_rate={wins / len(pnls) * 100:5.1f}% pnl=${sum(pnls):+.2f}")
    print(f"  {REBASELINE_NOTE}")

    _hdr("Per-agent stats (credited once per closed position since the 2026-11 fix; older rows are per-leg)")
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

    _hdr("Worst symbols (position-level avg pnl_pct, min 2 positions)")
    by_symbol: dict[str, list[dict]] = {}
    for p in collapse_to_positions(all_rows, open_keys):
        by_symbol.setdefault(p["symbol"], []).append(p)
    worst = sorted(
        ((sym, ps) for sym, ps in by_symbol.items() if len(ps) >= 2),
        key=lambda kv: sum(p["pnl_pct"] for p in kv[1]) / len(kv[1]),
    )[:10]
    for sym, ps in worst:
        print(f"  {sym:10s} n={len(ps):3d} avg={sum(p['pnl_pct'] for p in ps) / len(ps):+.2f}% "
              f"total=${sum(p['pnl'] for p in ps):.2f}")

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
