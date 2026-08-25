"""Post-exit price action analysis (Fix 10).

For every closed trade, looks at what price actually did in the days AFTER
the exit to answer: was this exit premature (price kept moving favorably
afterward — we left money on the table) or well-timed / even generous
(price reversed against the original direction, or barely moved)?

Classifies each trade's post-exit window (default 10 daily bars after
exit_ts) into:
  - "premature_exit"   price moved favorably beyond take_profit_1_pct after we
                        sold (would have kept running in our favor)
  - "well_timed"       price moved unfavorably beyond stop_loss_pct after we
                        sold (good thing we were out)
  - "neutral"          neither threshold was cleared in the window

Only meaningful for SELL-closing trades where "favorable direction" = up
(this strategy is spot/long-only). Uses whatever daily candles the repo
already has cached/fetchable (up to `--bars`, default 1000 ~= 2.7 years),
so trades near the very end of that window may have too little post-exit
history to classify (reported separately, not silently dropped).

Read-only — fetches public klines, never places orders.

    DATA_CACHE_DIR=/tmp/droplet_db python -m scripts.post_exit_analysis --mode live
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from app.config import Timeframe, get_settings
from app.data import OHLCVRepository


def _conn() -> sqlite3.Connection:
    db = get_settings().data_cache_dir / "trading.db"
    if not db.exists():
        raise SystemExit(f"no DB at {db}")
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _parse_ts(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _load_trades(mode: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM closed_trades WHERE mode=? ORDER BY exit_ts ASC", (mode,)
        ).fetchall()
    return [dict(r) for r in rows]


def classify(df: pd.DataFrame, exit_ts: datetime, exit_price: float, *, lookback_bars: int,
             tp_pct: float, sl_pct: float) -> str:
    after = df[df.index > pd.Timestamp(exit_ts)]
    if after.empty:
        return "insufficient_data"
    window = after.iloc[:lookback_bars]
    if len(window) < 2:
        return "insufficient_data"
    max_up = float((window["high"].max() - exit_price) / exit_price)
    max_down = float((exit_price - window["low"].min()) / exit_price)
    if max_up >= tp_pct:
        return "premature_exit"
    if max_down >= sl_pct:
        return "well_timed"
    return "neutral"


async def _run(mode: str, bars: int, lookback_bars: int) -> None:
    trades = _load_trades(mode)
    if not trades:
        raise SystemExit(f"no closed trades found for mode={mode}")
    s = get_settings()
    repo = OHLCVRepository()

    by_symbol: dict[str, pd.DataFrame] = {}
    counts: Counter[str] = Counter()
    by_exit_reason: dict[str, Counter[str]] = {}

    for t in trades:
        symbol = t["symbol"]
        exit_ts = _parse_ts(t.get("exit_ts"))
        if exit_ts is None:
            counts["insufficient_data"] += 1
            continue
        if symbol not in by_symbol:
            try:
                df = await repo.get(symbol, Timeframe.D1, limit=bars, refresh=False)
            except Exception as exc:  # noqa: BLE001
                print(f"  skip {symbol}: candle fetch failed ({exc})")
                by_symbol[symbol] = pd.DataFrame()
            else:
                by_symbol[symbol] = df
        df = by_symbol[symbol]
        if df.empty:
            counts["insufficient_data"] += 1
            continue
        outcome = classify(
            df, exit_ts, float(t["exit_price"]), lookback_bars=lookback_bars,
            tp_pct=s.take_profit_1_pct, sl_pct=s.stop_loss_pct,
        )
        counts[outcome] += 1
        reason = t.get("exit_reason") or "unknown"
        by_exit_reason.setdefault(reason, Counter())[outcome] += 1

    total = sum(counts.values())
    print(f"Post-exit price action — mode={mode} trades={len(trades)} classified={total} "
          f"(lookback={lookback_bars} daily bars, TP threshold={s.take_profit_1_pct:.1%}, "
          f"SL threshold={s.stop_loss_pct:.1%})")
    print()
    for outcome, n in counts.most_common():
        print(f"  {outcome:<20} {n:>5}  ({n / total:.1%})" if total else f"  {outcome}: {n}")
    print()
    print("By exit_reason:")
    for reason, sub in sorted(by_exit_reason.items(), key=lambda kv: -sum(kv[1].values())):
        n = sum(sub.values())
        premature = sub.get("premature_exit", 0)
        print(f"  {reason:<28} n={n:<5} premature_exit={premature:<5} "
              f"({premature / n:.1%})" if n else f"  {reason}: 0")
    print()
    print(
        "Interpretation: a high 'premature_exit' share for a specific exit_reason means that "
        "path is closing winners too early relative to where price actually went next — a "
        "candidate to loosen (wider trailing distance, higher TP1) rather than tighten."
    )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", default="live", choices=["live", "paper"])
    p.add_argument("--bars", type=int, default=1000)
    p.add_argument("--lookback-bars", type=int, default=10)
    args = p.parse_args()
    asyncio.run(_run(args.mode, args.bars, args.lookback_bars))


if __name__ == "__main__":
    main()
