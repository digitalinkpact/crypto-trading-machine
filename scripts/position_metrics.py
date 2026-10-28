"""Position-level performance metrics from closed_trades (read-only).

A partial take-profit ladder writes one `closed_trades` row per LEG
(take_profit_1, take_profit_2, trailing_stop ...), so row-level win rate
over-counts winners: a position that banks TP1 and then stops out on the
remainder is one position, not one win + one loss. This script groups legs
by (symbol, entry_ts) and reports what the operator actually cares about —
per-POSITION win rate, profit factor, expectancy (net of fees), average
win/loss, longest losing streak, and breakdowns by exit path, symbol, entry
strategy and BTC regime at entry.

    python -m scripts.position_metrics --mode live
    python -m scripts.position_metrics --mode live --since 2026-08-01
    python -m scripts.position_metrics --mode live --exclude-ids 646
    DATA_CACHE_DIR=./data/live_export python -m scripts.position_metrics --mode live

Never places trades or edits DB rows.
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from app.config import get_settings

REGIME_LABEL = {2: "STRONG_BULL", 1: "BULL", 0: "SIDEWAYS", -1: "BEAR", -2: "STRONG_BEAR"}


@dataclass
class Position:
    symbol: str
    entry_ts: str
    entry_price: float
    qty: float = 0.0
    pnl: float = 0.0
    gross_pnl: float = 0.0
    fees: float = 0.0
    legs: list[dict] = field(default_factory=list)
    exit_ts: str = ""
    entry_strategy: Optional[str] = None
    entry_btc_regime: Optional[int] = None
    entry_confidence: Optional[float] = None

    @property
    def notional(self) -> float:
        return self.entry_price * self.qty

    @property
    def pnl_pct(self) -> float:
        return (self.pnl / self.notional * 100.0) if self.notional else 0.0

    @property
    def exit_path(self) -> str:
        """Ordered, de-duplicated chain of leg exit reasons, e.g.
        'take_profit_1>trailing_stop'. Dust-cleanup legs are dropped."""
        chain: list[str] = []
        for leg in sorted(self.legs, key=lambda r: r["exit_ts"] or ""):
            reason = leg["exit_reason"] or "unknown"
            if reason.endswith("_stale_dust"):
                continue
            if not chain or chain[-1] != reason:
                chain.append(reason)
        return ">".join(chain) or "stale_dust_only"

    @property
    def final_exit(self) -> str:
        return self.exit_path.split(">")[-1]


def _conn(path) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def load_positions(
    mode: str,
    *,
    since: Optional[str] = None,
    until: Optional[str] = None,
    exclude_ids: Iterable[int] = (),
    db_path=None,
) -> list[Position]:
    db = db_path or (get_settings().data_cache_dir / "trading.db")
    excluded = set(exclude_ids)
    sql = "SELECT * FROM closed_trades WHERE mode=?"
    params: list = [mode]
    if since:
        sql += " AND entry_ts >= ?"
        params.append(since)
    if until:
        sql += " AND entry_ts < ?"
        params.append(until)
    sql += " ORDER BY entry_ts, exit_ts"
    with _conn(db) as c:
        rows = [dict(r) for r in c.execute(sql, params)]
    by_key: dict[tuple[str, str], Position] = {}
    for r in rows:
        if r["id"] in excluded:
            continue
        key = (r["symbol"], r["entry_ts"])
        pos = by_key.get(key)
        if pos is None:
            pos = by_key[key] = Position(
                symbol=r["symbol"], entry_ts=r["entry_ts"],
                entry_price=float(r["entry_price"] or 0),
                entry_strategy=r.get("entry_strategy"),
                entry_btc_regime=r.get("entry_btc_regime"),
                entry_confidence=r.get("entry_confidence"),
            )
        pos.qty += float(r["qty"] or 0)
        pos.pnl += float(r["pnl"] or 0)
        pos.gross_pnl += float(r["gross_pnl"] if r["gross_pnl"] is not None else r["pnl"] or 0)
        pos.fees += float(r["fee_amount"] or 0)
        pos.legs.append(r)
        pos.exit_ts = max(pos.exit_ts, r["exit_ts"] or "")
    return sorted(by_key.values(), key=lambda p: p.exit_ts)


def summarize(positions: list[Position]) -> dict:
    n = len(positions)
    if n == 0:
        return {"positions": 0}
    wins = [p for p in positions if p.pnl > 0]
    losses = [p for p in positions if p.pnl <= 0]
    gross_profit = sum(p.pnl for p in wins)
    gross_loss = -sum(p.pnl for p in losses)
    streak = worst = 0
    for p in positions:
        streak = streak + 1 if p.pnl <= 0 else 0
        worst = max(worst, streak)
    equity = peak = dd = 0.0
    for p in positions:
        equity += p.pnl
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return {
        "positions": n,
        "legs": sum(len(p.legs) for p in positions),
        "win_rate": len(wins) / n,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else float("inf"),
        "expectancy": sum(p.pnl for p in positions) / n,
        "expectancy_pct": sum(p.pnl_pct for p in positions) / n,
        "avg_win": (gross_profit / len(wins)) if wins else 0.0,
        "avg_loss": (-gross_loss / len(losses)) if losses else 0.0,
        "avg_win_pct": (sum(p.pnl_pct for p in wins) / len(wins)) if wins else 0.0,
        "avg_loss_pct": (sum(p.pnl_pct for p in losses) / len(losses)) if losses else 0.0,
        "total_pnl": sum(p.pnl for p in positions),
        "total_fees": sum(p.fees for p in positions),
        "max_losing_streak": worst,
        "max_drawdown_usdt": dd,
    }


def _fmt_summary(s: dict) -> str:
    if not s.get("positions"):
        return "  (no positions)"
    pf = s["profit_factor"]
    return (
        f"  positions={s['positions']} legs={s['legs']}\n"
        f"  win_rate={s['win_rate']:.1%}  profit_factor={pf if pf == float('inf') else round(pf, 2)}\n"
        f"  expectancy={s['expectancy']:+.4f} USDT/pos ({s['expectancy_pct']:+.2f}%)\n"
        f"  avg_win={s['avg_win']:+.4f} ({s['avg_win_pct']:+.2f}%)  "
        f"avg_loss={s['avg_loss']:+.4f} ({s['avg_loss_pct']:+.2f}%)\n"
        f"  total_pnl={s['total_pnl']:+.4f}  total_fees={s['total_fees']:.4f}\n"
        f"  max_losing_streak={s['max_losing_streak']}  max_drawdown={s['max_drawdown_usdt']:.4f} USDT"
    )


def _breakdown(title: str, positions: list[Position], key, *, min_n: int = 1) -> None:
    buckets: dict[str, list[Position]] = defaultdict(list)
    for p in positions:
        buckets[str(key(p))].append(p)
    rows = []
    for name, ps in buckets.items():
        if len(ps) < min_n:
            continue
        s = summarize(ps)
        rows.append((name, s))
    rows.sort(key=lambda r: r[1]["total_pnl"])
    print(f"\n{title}")
    print("=" * 96)
    print(f"{'bucket':<36} {'n':>4} {'win%':>7} {'PF':>6} {'exp%':>7} {'avgW%':>7} {'avgL%':>7} {'total_pnl':>10}")
    for name, s in rows:
        pf = s["profit_factor"]
        pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
        print(
            f"{name:<36} {s['positions']:>4} {s['win_rate']:>7.1%} {pf_s:>6} "
            f"{s['expectancy_pct']:>+7.2f} {s['avg_win_pct']:>+7.2f} {s['avg_loss_pct']:>+7.2f} "
            f"{s['total_pnl']:>+10.4f}"
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="live", choices=["live", "paper"])
    ap.add_argument("--since", help="ISO date: only positions ENTERED on/after")
    ap.add_argument("--until", help="ISO date: only positions ENTERED before")
    ap.add_argument("--exclude-ids", default="", help="comma-separated closed_trades.id to drop")
    ap.add_argument("--min-n", type=int, default=1, help="hide breakdown buckets with fewer positions")
    args = ap.parse_args()
    excluded = [int(x) for x in args.exclude_ids.split(",") if x.strip()]
    positions = load_positions(args.mode, since=args.since, until=args.until, exclude_ids=excluded)
    print(f"Position metrics — mode={args.mode} since={args.since or '-'} until={args.until or '-'} "
          f"excluded_ids={excluded or '-'}  generated={datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(_fmt_summary(summarize(positions)))
    _breakdown("By final exit", positions, lambda p: p.final_exit, min_n=args.min_n)
    _breakdown("By exit path", positions, lambda p: p.exit_path, min_n=args.min_n)
    _breakdown("By entry strategy", positions, lambda p: p.entry_strategy or "(none)", min_n=args.min_n)
    _breakdown(
        "By BTC regime at entry", positions,
        lambda p: REGIME_LABEL.get(p.entry_btc_regime, "(unknown)"), min_n=args.min_n,
    )
    _breakdown("By symbol", positions, lambda p: p.symbol, min_n=args.min_n)


if __name__ == "__main__":
    main()
