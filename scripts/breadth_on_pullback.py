"""Breadth gate validation on the PULLBACK entry (the live 94% entry).
READ-ONLY backtest. Trades are re-dated from fold-relative entry_idx via
_fold_bounds so calendar windows slice correctly.
"""
from __future__ import annotations
import asyncio, glob, sys
import pandas as pd
sys.path.insert(0, "/opt/crypto-trading-machine")
from app.config import Timeframe
from app.data import OHLCVRepository
import scripts.strategy_lab as L

WIN  = (pd.Timestamp("2026-09-14", tz="UTC"), pd.Timestamp("2026-09-29", tz="UTC"))
LOSE = (pd.Timestamp("2026-10-05", tz="UTC"), pd.Timestamp("2026-10-28", tz="UTC"))

def collect_dated(frames, entry_fn, params, *, folds, risk_on_fn):
    out = []
    for sym, df in frames.items():
        risk_on = risk_on_fn(frames, sym)
        for lo, hi in L._fold_bounds(len(df), folds):
            sub = df.iloc[lo:hi]
            if len(sub) < 40:
                continue
            sub_r = risk_on.iloc[lo:hi] if risk_on is not None else None
            for t in L.simulate_symbol(sym, sub, entry_fn, params, risk_on=sub_r):
                ts = pd.Timestamp(sub.index[t.entry_idx])
                if ts.tzinfo is None:
                    ts = ts.tz_localize("UTC")
                out.append((t, ts))
    return out

def metrics(pairs, a=None, b=None):
    return L.aggregate_positions([t for t, ts in pairs if a is None or (a <= ts < b)])

async def load_universe(limit_bars=400):
    syms = sorted({p.split("/")[-1].removesuffix("_1d.pkl") for p in glob.glob("data/cache/*_1d.pkl")})
    return await L._load(OHLCVRepository(), Timeframe.D1, limit_bars, syms)

async def main():
    frames = await load_universe()
    last = max(str(df.index[-1])[:10] for df in frames.values())
    print(f"loaded {len(frames)} symbols (droplet dynamic universe, data through {last})")
    params = L.default_params(); entry = L.entry_pullback
    variants = {
        "btc_regime (CURRENT live gate)": L._risk_on_series,
        "breadth_ema50>=0.30":           L._breadth_risk_on_fn(50, 0.30),
        "breadth_ema50>=0.40":           L._breadth_risk_on_fn(50, 0.40),
        "breadth_ema50>=0.50":           L._breadth_risk_on_fn(50, 0.50),
        "breadth_ema20>=0.40":           L._breadth_risk_on_fn(20, 0.40),
        "breadth_ema20>=0.50":           L._breadth_risk_on_fn(20, 0.50),
        "btc AND breadth50>=0.40":       L._breadth_risk_on_fn(50, 0.40, with_btc=True),
        "btc AND breadth50>=0.50":       L._breadth_risk_on_fn(50, 0.50, with_btc=True),
        "btc AND breadth20>=0.50":       L._breadth_risk_on_fn(20, 0.50, with_btc=True),
    }
    f = lambda m: f"n={m.n:4d} win={m.win_rate:5.1%} pf={min(m.profit_factor,99):5.2f} exp={m.expectancy_pct:+.2%}"
    print(f"\n{'gate':32s} | {'SEP 14-28 (won live 82%)':>40s} | {'OCT 05-27 (lost live 36%)':>40s} | {'ALL pooled':>40s}")
    print("-" * 165)
    rows = []
    for name, fn in variants.items():
        pairs = collect_dated(frames, entry, params, folds=3, risk_on_fn=fn)
        w, l, p = metrics(pairs, *WIN), metrics(pairs, *LOSE), metrics(pairs)
        print(f"{name:32s} | {f(w):>40s} | {f(l):>40s} | {f(p):>40s}")
        rows.append((name, w, l, p))
    base = rows[0]
    print("\n=== verdict vs CURRENT gate  (keep_sep: SEP n>=60% & pf>=1.2 | fix_oct: OCT n<=40% or pf>=1.0 | better: pooled pf & exp up) ===")
    for name, w, l, p in rows[1:]:
        keep = (w.n >= 0.6 * max(base[1].n, 1)) and (w.profit_factor >= 1.2)
        fix = (l.n <= 0.4 * max(base[2].n, 1)) or (l.profit_factor >= 1.0)
        better = (p.profit_factor > base[3].profit_factor) and (p.expectancy_pct > base[3].expectancy_pct)
        tag = "  <== CANDIDATE" if (keep and fix and better) else ""
        print(f"  {name:32s} keep_sep={keep!s:5s} fix_oct={fix!s:5s} better={better!s:5s}{tag}")

asyncio.run(main())
