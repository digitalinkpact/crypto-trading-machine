"""Find any window / slice in live history where win-rate reached a target.
READ-ONLY. Usage: python find_winrate.py --target 0.90 --db data/cache/trading.db
"""
from __future__ import annotations
import argparse, sqlite3, collections, datetime as dt

p = argparse.ArgumentParser(); p.add_argument("--db", default="data/cache/trading.db"); p.add_argument("--target", type=float, default=0.90)
a = p.parse_args()
c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True); c.row_factory = sqlite3.Row
rows = [dict(r) for r in c.execute("SELECT id,symbol,pnl,entry_ts,exit_ts,exit_reason,entry_strategy,entry_btc_regime,agents FROM closed_trades WHERE mode='live' ORDER BY exit_ts")]
print(f"live closed_trades: {len(rows)}  span {rows[0]['exit_ts'][:10]} -> {rows[-1]['exit_ts'][:10]}")

def wr(xs): 
    n=len(xs); w=sum(1 for x in xs if (x['pnl'] or 0)>0); return n, (w/n if n else 0.0), sum(x['pnl'] or 0 for x in xs)

print("\n=== BEST rolling window win-rate (min 20 trades) ===")
best=[]
for N in (20,30,50,100):
    top=(0,None,0,0)
    for i in range(0,len(rows)-N+1):
        n,r,pnl=wr(rows[i:i+N])
        if r>top[0]: top=(r,i,n,pnl)
    r,i,n,pnl=top
    if i is not None:
        print(f"  N={N:3d}: best win={r:5.1%}  pnl=${pnl:+8.2f}  {rows[i]['exit_ts'][:10]} -> {rows[i+N-1]['exit_ts'][:10]}")

print("\n=== BEST calendar week (min 10 trades) ===")
byw=collections.defaultdict(list)
for x in rows:
    d=dt.date.fromisoformat(x['exit_ts'][:10]); byw[d.isocalendar()[:2]].append(x)
for k,xs in sorted(byw.items()):
    n,r,pnl=wr(xs)
    flag=" <== >=target" if r>=a.target and n>=10 else ""
    print(f"  {k[0]}-W{k[1]:02d} n={n:4d} win={r:5.1%} pnl=${pnl:+8.2f}{flag}")

print("\n=== by entry_strategy (all time) ===")
bys=collections.defaultdict(list)
for x in rows: bys[x['entry_strategy'] or 'unknown'].append(x)
for k,xs in sorted(bys.items(), key=lambda kv:-len(kv[1])):
    n,r,pnl=wr(xs); print(f"  {k:18s} n={n:4d} win={r:5.1%} pnl=${pnl:+8.2f}")

print("\n=== by exit_reason (all time) — 'win rate' people quote is often THIS ===")
bye=collections.defaultdict(list)
for x in rows: bye[x['exit_reason'] or 'unknown'].append(x)
for k,xs in sorted(bye.items(), key=lambda kv:-len(kv[1])):
    n,r,pnl=wr(xs); print(f"  {k:28s} n={n:4d} win={r:5.1%} pnl=${pnl:+8.2f}")

print("\n=== by BTC regime at entry (all time) ===")
byr=collections.defaultdict(list)
for x in rows: byr[str(x['entry_btc_regime'])].append(x)
for k,xs in sorted(byr.items()):
    n,r,pnl=wr(xs); print(f"  regime={k:5s} n={n:4d} win={r:5.1%} pnl=${pnl:+8.2f}")

print("\n=== equity: peak and path ===")
eq=[dict(r) for r in c.execute("SELECT ts,total_usdt FROM equity_snapshots WHERE mode='live' ORDER BY id")]
pk=max(eq,key=lambda e:e['total_usdt'])
print(f"  first {eq[0]['ts'][:10]} ${eq[0]['total_usdt']:.2f} | PEAK {pk['ts'][:10]} ${pk['total_usdt']:.2f} | now ${eq[-1]['total_usdt']:.2f}")
# monthly
bym=collections.OrderedDict()
for e in eq: bym.setdefault(e['ts'][:7],[]).append(e['total_usdt'])
for m,v in bym.items(): print(f"  {m}: start ${v[0]:8.2f} end ${v[-1]:8.2f} ({(v[-1]/v[0]-1)*100:+.1f}%)")
