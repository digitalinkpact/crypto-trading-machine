"""Daily forensic report: which conditions are actually making/losing money.

Breaks down realized trade performance by BTC regime (recomputed via the
scored -2..+2 regime, not just a binary flag), symbol, exit reason, and hour
of day, then calls out the top 3 profitable and bottom 3 losing conditions —
answering "what type of trade is actually making money?" per the forensic
audit process. Read-only: never places trades or edits DB rows.

Run from repo root:

    python -m scripts.daily_forensic_report               # trailing 24h
    python -m scripts.daily_forensic_report --days 7       # trailing 7 days
    python -m scripts.daily_forensic_report --mode live
"""
from __future__ import annotations

import argparse
import asyncio
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from app.config import Timeframe, get_settings
from app.data import OHLCVRepository
from app.regime.btc_regime import compute_btc_regime_score
from app.ta import add_indicators


def _conn() -> sqlite3.Connection:
    db = get_settings().data_cache_dir / "trading.db"
    if not db.exists():
        raise SystemExit(f"no DB at {db}")
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _hdr(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _parse_ts(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _btc_regime_by_day() -> dict:
    """Regime score for every day BTC has daily OHLCV history for."""
    df = await OHLCVRepository().get("BTCUSDT", Timeframe.D1, refresh=False)
    df = add_indicators(df).dropna()
    out: dict = {}
    for i in range(len(df)):
        window = df.iloc[: i + 1]
        result = compute_btc_regime_score(window)
        day = pd.Timestamp(df.index[i]).date()
        out[day] = result
    return out


def _bucket_stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "win_rate": 0.0, "total_pnl": 0.0, "avg_pct": 0.0}
    wins = [r for r in rows if r["pnl"] > 0]
    return {
        "n": len(rows),
        "win_rate": len(wins) / len(rows) * 100,
        "total_pnl": sum(r["pnl"] for r in rows),
        "avg_pct": statistics.mean(r["pnl_pct"] for r in rows),
    }


def _print_table(title: str, buckets: dict[str, list[dict]]) -> None:
    _hdr(title)
    rows = [(k, _bucket_stats(v)) for k, v in buckets.items()]
    rows.sort(key=lambda kv: kv[1]["total_pnl"])
    print(f"{'condition':<28} {'n':>5} {'win%':>7} {'avg%':>8} {'total_pnl':>12}")
    for label, stat in rows:
        print(
            f"{label:<28} {stat['n']:>5} {stat['win_rate']:>6.1f}% "
            f"{stat['avg_pct']:>7.2f}% {stat['total_pnl']:>12.4f}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=1, help="trailing window in days (default: 1)")
    ap.add_argument("--mode", default="live", choices=["live", "paper"])
    args = ap.parse_args()

    c = _conn()
    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    trades = [
        dict(r)
        for r in c.execute(
            "SELECT * FROM closed_trades WHERE mode=? ORDER BY entry_ts ASC", (args.mode,)
        ).fetchall()
    ]
    trades = [t for t in trades if (_parse_ts(t.get("entry_ts")) or cutoff) >= cutoff]

    _hdr(f"Daily forensic report — mode={args.mode} window={args.days}d trades={len(trades)}")
    if not trades:
        print("(no closed trades in this window)")
        return

    overall = _bucket_stats(trades)
    print(
        f"win_rate={overall['win_rate']:.1f}% avg_pct={overall['avg_pct']:.2f}% "
        f"total_pnl={overall['total_pnl']:.4f}"
    )

    regime_by_day = asyncio.run(_btc_regime_by_day())

    by_regime: dict[str, list[dict]] = defaultdict(list)
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    by_exit_reason: dict[str, list[dict]] = defaultdict(list)
    by_hour: dict[str, list[dict]] = defaultdict(list)

    for t in trades:
        entry_ts = _parse_ts(t.get("entry_ts"))
        if entry_ts is not None:
            day = entry_ts.date()
            result = regime_by_day.get(day)
            label = result.label if result else "unknown"
            by_regime[label].append(t)
            by_hour[f"{entry_ts.hour:02d}:00 UTC"].append(t)
        by_symbol[t["symbol"]].append(t)
        by_exit_reason[t.get("exit_reason") or "unknown"].append(t)

    _print_table("By BTC regime at entry", by_regime)
    _print_table("By symbol", by_symbol)
    _print_table("By exit reason", by_exit_reason)
    _print_table("By hour of day (UTC)", by_hour)

    # Top/bottom conditions across ALL dimensions combined (min 2 trades so a
    # single lucky/unlucky trade can't dominate the ranking).
    combined: dict[str, list[dict]] = {}
    for prefix, buckets in (
        ("regime", by_regime), ("symbol", by_symbol),
        ("exit", by_exit_reason), ("hour", by_hour),
    ):
        for label, rows in buckets.items():
            if len(rows) >= 2:
                combined[f"{prefix}:{label}"] = rows

    ranked = sorted(combined.items(), key=lambda kv: _bucket_stats(kv[1])["total_pnl"])
    _hdr("Top 3 profitable conditions")
    for label, rows in ranked[-3:][::-1]:
        st = _bucket_stats(rows)
        print(f"  {label:<28} n={st['n']:<4} win={st['win_rate']:.1f}% pnl={st['total_pnl']:.4f}")
    _hdr("Bottom 3 losing conditions")
    for label, rows in ranked[:3]:
        st = _bucket_stats(rows)
        print(f"  {label:<28} n={st['n']:<4} win={st['win_rate']:.1f}% pnl={st['total_pnl']:.4f}")


if __name__ == "__main__":
    main()
