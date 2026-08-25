"""Drawdown circuit-breaker threshold sweep (Fix 7).

Replays the REAL sequence of closed trades (chronological by exit_ts) against
a simple running-equity simulation to answer: at what
`drawdown_circuit_breaker_pct` would new-BUY halts have actually engaged, how
much of the observed loss would have been avoided, and how many (real,
already-placed) trades would have been skipped as a result?

This is a sizing/safety-parameter sweep, NOT a strategy backtest — it reuses
the trades that actually happened; it does not simulate what the bot would
have done differently once halted (no counterfactual re-entries are modeled).
That means the "avoided" totals below are a lower bound on the benefit of a
tighter breaker (skipped trades cannot compound into further losses, but any
of the bot's later winning trades occurring during a hypothetical halt window
are also excluded from the "if halted" curve).

Read-only. Point at an alternate/copied DB via DATA_CACHE_DIR, e.g.:

    DATA_CACHE_DIR=/tmp/droplet_db python -m scripts.drawdown_threshold_sweep --mode live

Run from repo root:

    python -m scripts.drawdown_threshold_sweep --mode live --starting-balance 400
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import get_settings

DEFAULT_THRESHOLDS = (0.10, 0.12, 0.15, 0.18, 0.20, 0.25)


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
    trades = [dict(r) for r in rows]
    trades.sort(key=lambda t: _parse_ts(t.get("exit_ts")) or datetime.min.replace(tzinfo=timezone.utc))
    return trades


def simulate(trades: list[dict], *, starting_balance: float, threshold_pct: float) -> dict:
    """Replay trades in order; once cumulative drawdown from peak equity
    breaches `threshold_pct`, all SUBSEQUENT trades are treated as skipped
    (the circuit breaker would have blocked the new entry that produced
    them) until equity recovers back above the trip line."""
    equity = starting_balance
    peak = starting_balance
    halted = False
    halt_events = 0
    included = 0
    skipped = 0
    max_dd = 0.0
    for t in trades:
        pnl = float(t.get("pnl") or 0.0)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
        if not halted and dd >= threshold_pct:
            halted = True
            halt_events += 1
        if halted:
            # Recovery check: once equity claws back above the trip line
            # (peak * (1-threshold)), allow new entries again.
            if equity >= peak * (1 - threshold_pct):
                halted = False
            else:
                skipped += 1
                continue
        equity += pnl
        included += 1
        peak = max(peak, equity)
    total_return_pct = ((equity - starting_balance) / starting_balance * 100) if starting_balance else 0.0
    return {
        "threshold_pct": threshold_pct,
        "final_equity": equity,
        "total_return_pct": total_return_pct,
        "max_drawdown_pct": max_dd * 100,
        "halt_events": halt_events,
        "trades_included": included,
        "trades_skipped": skipped,
    }


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", default="live", choices=["live", "paper"])
    p.add_argument("--starting-balance", type=float, default=400.0)
    p.add_argument("--thresholds", type=float, nargs="*", default=list(DEFAULT_THRESHOLDS))
    args = p.parse_args()

    trades = _load_trades(args.mode)
    if not trades:
        raise SystemExit(f"no closed trades found for mode={args.mode}")

    print(f"Drawdown circuit-breaker sweep — mode={args.mode} trades={len(trades)} "
          f"starting_balance={args.starting_balance:.2f}")
    print(f"current live default: drawdown_circuit_breaker_pct = "
          f"{get_settings().drawdown_circuit_breaker_pct}")
    print()
    header = f"{'threshold':>10} {'final_equity':>13} {'return%':>9} {'max_dd%':>9} {'halts':>6} {'included':>9} {'skipped':>8}"
    print(header)
    print("-" * len(header))
    results = []
    for th in sorted(args.thresholds):
        r = simulate(trades, starting_balance=args.starting_balance, threshold_pct=th)
        results.append(r)
        print(
            f"{r['threshold_pct']:>9.0%} {r['final_equity']:>13.2f} {r['total_return_pct']:>8.2f}% "
            f"{r['max_drawdown_pct']:>8.2f}% {r['halt_events']:>6} {r['trades_included']:>9} {r['trades_skipped']:>8}"
        )

    best = max(results, key=lambda r: r["total_return_pct"])
    print()
    print(f"Best total-return threshold in this replay: {best['threshold_pct']:.0%} "
          f"(return={best['total_return_pct']:.2f}%, max_dd={best['max_drawdown_pct']:.2f}%)")
    print(
        "NOTE: this is a REPLAY of trades that already happened under whatever breaker was live "
        "at the time — it shows what a different threshold would have skipped, not a full "
        "counterfactual re-simulation. Treat as directional evidence, not a precise backtest."
    )


if __name__ == "__main__":
    main()
