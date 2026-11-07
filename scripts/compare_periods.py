"""Compare the WINNING period vs the LOSING period on every dimension the DB
records, to find what changed. READ-ONLY."""
from __future__ import annotations
import sqlite3, collections, json, subprocess

c = sqlite3.connect("file:data/cache/trading.db?mode=ro", uri=True); c.row_factory = sqlite3.Row
def rows(a, b):
    return [dict(r) for r in c.execute(
        "SELECT * FROM closed_trades WHERE mode='live' AND exit_ts>=? AND exit_ts<? ORDER BY exit_ts", (a, b))]

WIN  = rows("2026-09-14", "2026-09-29")   # W37-W39 ramp + peak
LOSE = rows("2026-10-05", "2026-10-25")   # W41-W43 bleed

def summary(label, xs):
    n=len(xs); w=sum(1 for x in xs if (x['pnl'] or 0)>0); pnl=sum(x['pnl'] or 0 for x in xs)
    print(f"\n##### {label}: n={n} win={w/max(n,1):.1%} pnl=${pnl:+.2f}")
    for col in ("entry_strategy","exit_reason","entry_btc_regime","symbol"):
        d=collections.defaultdict(lambda:[0,0,0.0])
        for x in xs:
            k=str(x.get(col)); d[k][0]+=1; d[k][1]+= (x['pnl'] or 0)>0; d[k][2]+= x['pnl'] or 0
        top=sorted(d.items(), key=lambda kv:-kv[1][0])[:8]
        print(f"  by {col}:")
        for k,(nn,ww,pp) in top: print(f"    {k[:26]:26s} n={nn:4d} win={ww/nn:5.1%} pnl=${pp:+8.2f}")
    hrs=[x['holding_hours'] for x in xs if x.get('holding_hours') is not None]
    conf=[x['entry_confidence'] for x in xs if x.get('entry_confidence') is not None]
    notional=[(x['qty'] or 0)*(x['entry_price'] or 0) for x in xs]
    mfe=[x['mfe_pct'] for x in xs if x.get('mfe_pct') is not None]; mae=[x['mae_pct'] for x in xs if x.get('mae_pct') is not None]
    avg=lambda v: (sum(v)/len(v)) if v else float('nan')
    print(f"  avg hold={avg(hrs):.1f}h  avg conf={avg(conf):.3f}  avg notional=${avg(notional):.2f}  avg MFE={avg(mfe):+.2%}  avg MAE={avg(mae):+.2%}")
    wins=[x['pnl_pct'] for x in xs if (x['pnl'] or 0)>0 and x.get('pnl_pct') is not None]
    loss=[x['pnl_pct'] for x in xs if (x['pnl'] or 0)<0 and x.get('pnl_pct') is not None]
    print(f"  avg win%={avg(wins):+.2%} (n={len(wins)})  avg loss%={avg(loss):+.2%} (n={len(loss)})")
    # distinct symbols traded
    print(f"  distinct symbols: {len(set(x['symbol'] for x in xs))}")
    # agents field sample (what signal/strategy produced entries)
    ag=collections.Counter()
    for x in xs:
        try:
            for a in json.loads(x['agents'] or '[]'): ag[str(a)[:30]]+=1
        except Exception: ag[str(x['agents'])[:30]]+=1
    print("  top agents tags:", ag.most_common(6))

summary("WINNING  2026-09-14..09-28", WIN)
summary("LOSING   2026-10-05..10-24", LOSE)

print("\n##### git commits deployed between the two periods (what changed in code) #####")
out = subprocess.run('git log --since=2026-09-26 --until=2026-10-13 --format="%h %ad %s" --date=short -- app/ | head -40', shell=True, capture_output=True, text=True).stdout
print(out or "(none)")
print("##### .env trading flags currently set (names only) #####")
out = subprocess.run("grep -E '^(ENTRY_STRATEGY|PULLBACK_ENTRY_ENABLED|MARKET_REGIME_GATE_ENABLED|REQUIRE_STRONG_BULL_REGIME|TREND_FILTER_ENABLED|MAX_OPEN_POSITIONS|ROLLBACK_MAX_OPEN_POSITIONS|AGGRESSIVE_MAX_OPEN_POSITIONS|ROLLBACK_POSITION_PCT|AGGRESSIVE_POSITION_PCT|AGGRESSIVE_MODE_ENABLED|MIN_SIGNAL_CONFIDENCE|UNIVERSE_SIZE|FINAL_PAIRLIST_SIZE|BLOCKED_SYMBOLS|ML_GATE_ENABLED|LIVE_BUYS_ENABLED|DAILY_LOSS_LIMIT_PCT|EXCHANGE_NATIVE_STOPS_ENABLED|EXCHANGE_NATIVE_STOPS_CANARY_SYMBOLS)=' .env", shell=True, capture_output=True, text=True).stdout
print(out or "(none overridden)")
