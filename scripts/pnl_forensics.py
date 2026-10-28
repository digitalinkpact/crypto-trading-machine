"""Production loss forensics (READ-ONLY). Run on the droplet against trading.db.

    python -m scripts.pnl_forensics [--db PATH] [--days 30]

Never writes. Never prints secrets.
"""
from __future__ import annotations

import argparse
import collections
import json
import sqlite3
from pathlib import Path


def _kv(c, k):
    r = c.execute("SELECT value FROM kv WHERE key=?", (k,)).fetchone()
    if not r:
        return None
    try:
        return json.loads(r[0])
    except Exception:
        return r[0]


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="data/cache/trading.db")
    p.add_argument("--days", type=int, default=30)
    a = p.parse_args()
    db = Path(a.db)
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    W = f"-{a.days} day"

    st = _kv(c, "autopilot_state") or {}
    print("=== STATE ===")
    print({k: st.get(k) for k in ("running", "mode", "trades_executed", "last_error", "starting_balance_usdt")})
    print("halt:", _kv(c, "emergency_halt"))
    print("entry:", (_kv(c, "entry_status") or {}).get("reasons"))
    print("last tick by_reason:", (_kv(c, "autopilot_last_tick_debug") or {}).get("by_reason"))

    cols = [r[1] for r in c.execute("PRAGMA table_info(closed_trades)")]
    print("\n=== closed_trades columns ===")
    print(cols)

    print("\n=== REALIZED PnL by window (live) ===")
    for lbl, d in (("1d", 1), ("3d", 3), ("7d", 7), ("14d", 14), ("30d", 30), ("60d", 60), ("all", 9999)):
        r = c.execute(
            "SELECT COUNT(*) n, ROUND(SUM(pnl),2) pnl, SUM(pnl>0) w, "
            "ROUND(SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END),2) gp, "
            "ROUND(SUM(CASE WHEN pnl<0 THEN -pnl ELSE 0 END),2) gl "
            "FROM closed_trades WHERE mode='live' AND exit_ts>=datetime('now',?)",
            (f"-{d} day",),
        ).fetchone()
        n = r["n"] or 0
        pf = (r["gp"] / r["gl"]) if r["gl"] else float("inf")
        print(f"  {lbl:4s} n={n:5d} pnl=${(r['pnl'] or 0):9.2f} win={100*(r['w'] or 0)/max(n,1):5.1f}% PF={pf:5.2f}")

    print(f"\n=== {a.days}d by ENTRY STRATEGY ===")
    if "entry_strategy" in cols:
        q = ("SELECT entry_strategy t, COUNT(*) n, ROUND(SUM(pnl),2) pnl, SUM(pnl>0) w, ROUND(AVG(pnl),3) avg "
             "FROM closed_trades WHERE mode='live' AND exit_ts>=datetime('now',?) GROUP BY t ORDER BY pnl")
        for r in c.execute(q, (W,)):
            print(f"  {str(r['t']):20s} n={r['n']:4d} pnl=${r['pnl']:8.2f} win={100*r['w']/r['n']:5.1f}% avg=${r['avg']:+.3f}")
    else:
        agg = collections.defaultdict(lambda: [0, 0.0, 0])
        for r in c.execute("SELECT agents, pnl FROM closed_trades WHERE mode='live' AND exit_ts>=datetime('now',?)", (W,)):
            s = str(r["agents"] or "")
            k = "pullback" if "pullback" in s else ("oversold_bounce" if ("oversold" in s or "dip" in s) else "other")
            agg[k][0] += 1; agg[k][1] += r["pnl"] or 0; agg[k][2] += (r["pnl"] or 0) > 0
        for k, (n, pnl, w) in sorted(agg.items(), key=lambda x: x[1][1]):
            print(f"  {k:20s} n={n:4d} pnl=${pnl:8.2f} win={100*w/max(n,1):5.1f}%   (from agents json)")

    print(f"\n=== {a.days}d by EXIT REASON ===")
    pct = "ROUND(AVG(pnl_pct),4) avgpct" if "pnl_pct" in cols else "NULL avgpct"
    for r in c.execute(f"SELECT exit_reason, COUNT(*) n, ROUND(SUM(pnl),2) pnl, SUM(pnl>0) w, {pct} "
                       "FROM closed_trades WHERE mode='live' AND exit_ts>=datetime('now',?) GROUP BY exit_reason ORDER BY pnl", (W,)):
        print(f"  {str(r['exit_reason']):28s} n={r['n']:4d} pnl=${r['pnl']:8.2f} win={100*r['w']/r['n']:5.1f}% avg%={r['avgpct']}")

    print(f"\n=== {a.days}d by SYMBOL (worst 10) ===")
    for r in c.execute("SELECT symbol, COUNT(*) n, ROUND(SUM(pnl),2) pnl, SUM(pnl>0) w FROM closed_trades "
                       "WHERE mode='live' AND exit_ts>=datetime('now',?) GROUP BY symbol ORDER BY pnl LIMIT 10", (W,)):
        print(f"  {r['symbol']:12s} n={r['n']:3d} pnl=${r['pnl']:8.2f} win={100*r['w']/r['n']:5.1f}%")

    if "entry_btc_regime" in cols:
        print(f"\n=== {a.days}d by BTC REGIME at entry ===")
        for r in c.execute("SELECT entry_btc_regime rg, COUNT(*) n, ROUND(SUM(pnl),2) pnl, SUM(pnl>0) w FROM closed_trades "
                           "WHERE mode='live' AND exit_ts>=datetime('now',?) GROUP BY rg ORDER BY rg", (W,)):
            print(f"  regime={str(r['rg']):5s} n={r['n']:4d} pnl=${r['pnl']:8.2f} win={100*r['w']/max(r['n'],1):5.1f}%")

    print(f"\n=== {a.days}d HOLD TIME / SIZE ===")
    r = c.execute("SELECT ROUND(AVG((julianday(exit_ts)-julianday(entry_ts))*24),1) hrs, "
                  "ROUND(AVG(qty*entry_price),2) notional, ROUND(AVG(ABS(pnl)),3) avgabs, "
                  "ROUND(AVG(CASE WHEN pnl>0 THEN pnl END),3) avgwin, ROUND(AVG(CASE WHEN pnl<0 THEN pnl END),3) avgloss "
                  "FROM closed_trades WHERE mode='live' AND exit_ts>=datetime('now',?)", (W,)).fetchone()
    print(" ", dict(r))

    print("\n=== EQUITY ===")
    rows = c.execute("SELECT ts,total_usdt FROM equity_snapshots WHERE mode='live' ORDER BY id").fetchall()
    for lbl, i in (("first", 0), ("-30d", -720), ("-7d", -168), ("-1d", -24), ("latest", -1)):
        try:
            print(f"  {lbl:6s} {rows[i]['ts'][:16]} ${rows[i]['total_usdt']:.2f}")
        except Exception:
            pass

    print("\n=== OPEN POSITIONS (live) ===")
    for r in c.execute("SELECT symbol,qty,entry_price,entry_ts FROM positions WHERE mode='live' ORDER BY entry_ts"):
        print(f"  {r['symbol']:12s} entry={r['entry_price']} since={str(r['entry_ts'])[:16]}")

    print("\n=== daily realized pnl, last 14 days ===")
    for r in c.execute("SELECT date(exit_ts) d, COUNT(*) n, ROUND(SUM(pnl),2) pnl FROM closed_trades "
                       "WHERE mode='live' AND exit_ts>=datetime('now','-14 day') GROUP BY d ORDER BY d"):
        print(f"  {r['d']}  n={r['n']:3d}  ${r['pnl']:8.2f}")


if __name__ == "__main__":
    main()
