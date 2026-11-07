"""Single definition of "win rate" for the bot, its reports and its dashboards.

Two numbers exist and must never be confused:

* POSITION-level (the target): `closed_trades` legs are grouped by
  (symbol, mode, entry_ts); a position wins iff the SUM of its legs' fee-adjusted
  pnl is > 0. Book-keeping closes (phantom rows closed at the entry price) and
  positions still open after a TP1/TP2 partial are excluded. This is what the
  aggressive-mode rollback guard and the analytics snapshot use
  (`Settings.win_rate_position_level`).
* ROW-level (legacy, diagnostic only): every `closed_trades` row counted on its
  own. Winners scale out over several rows (TP1/TP2/trailing) while losers exit
  in one, so this reads several points too high (live 2026-10-12: 63% row vs
  33% position).

`Storage.closed_positions` is the SQL twin of `collapse_to_positions`; keep the
two in lock-step (tests/test_win_rate.py pins that).
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

REBASELINE_NOTE = (
    "Target is position-level. Historical 94-95% claims must be re-validated "
    "under this definition."
)


def is_bookkeeping_exit(reason: Optional[str]) -> bool:
    """Phantom/stale rows closed at entry to clear the book — not trades."""
    r = str(reason or "")
    return r == "reconcile_stale" or r.endswith("_stale_dust")


def open_position_keys(positions: Iterable[dict]) -> set[tuple[str, str, str]]:
    return {
        (str(p.get("symbol") or ""), str(p.get("mode") or ""), str(p.get("entry_ts") or ""))
        for p in positions
    }


def collapse_to_positions(
    rows: Iterable[dict], open_keys: Optional[set[tuple[str, str, str]]] = None
) -> list[dict]:
    """Group closed_trades legs into closed positions, oldest first.

    Each result is the LAST leg's row (so exit_reason/exit_ts/mfe/mae describe
    how the position ended) with `pnl`, `gross_pnl`, `pnl_pct` summed over all
    legs and `legs` = number of rows.
    """
    open_keys = open_keys or set()
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for r in rows:
        if is_bookkeeping_exit(r.get("exit_reason")):
            continue
        key = (str(r.get("symbol") or ""), str(r.get("mode") or ""), str(r.get("entry_ts") or ""))
        if key in open_keys:
            continue
        grouped.setdefault(key, []).append(r)

    out: list[dict] = []
    for legs in grouped.values():
        legs = sorted(legs, key=lambda r: r.get("id") or 0)
        pnl = sum(float(r.get("pnl") or 0.0) for r in legs)
        gross = sum(
            float(r["gross_pnl"]) if r.get("gross_pnl") is not None else float(r.get("pnl") or 0.0)
            for r in legs
        )
        notional = sum(float(r.get("entry_price") or 0.0) * float(r.get("qty") or 0.0) for r in legs)
        merged = dict(legs[-1])
        merged["entry_ts"] = legs[0].get("entry_ts")
        merged["pnl"] = pnl
        merged["gross_pnl"] = gross
        merged["pnl_pct"] = (pnl / notional * 100.0) if notional else 0.0
        merged["legs"] = len(legs)
        out.append(merged)
    out.sort(key=lambda p: p.get("id") or 0)
    return out


def win_rate_summary(
    rows: list[dict], open_keys: Optional[set[tuple[str, str, str]]] = None
) -> dict[str, Any]:
    """Both win rates (fractions 0..1) plus what was excluded and why."""
    positions = collapse_to_positions(rows, open_keys)
    pos_wins = sum(1 for p in positions if p["pnl"] > 0)
    row_wins = sum(1 for r in rows if float(r.get("pnl") or 0.0) > 0)
    open_keys = open_keys or set()
    excluded_open = len({
        (str(r.get("symbol") or ""), str(r.get("mode") or ""), str(r.get("entry_ts") or ""))
        for r in rows
        if not is_bookkeeping_exit(r.get("exit_reason"))
        and (str(r.get("symbol") or ""), str(r.get("mode") or ""), str(r.get("entry_ts") or "")) in open_keys
    })
    return {
        "position_n": len(positions),
        "position_wins": pos_wins,
        "position_win_rate": (pos_wins / len(positions)) if positions else 0.0,
        "row_n": len(rows),
        "row_wins": row_wins,
        "row_win_rate": (row_wins / len(rows)) if rows else 0.0,
        "excluded_bookkeeping_rows": sum(1 for r in rows if is_bookkeeping_exit(r.get("exit_reason"))),
        "excluded_open_positions": excluded_open,
        # Gross-positive positions that net out <= 0 after fees: wins that a
        # fee-blind measure would have claimed.
        "fee_flipped_positions": sum(1 for p in positions if p["gross_pnl"] > 0 >= p["pnl"]),
        "note": REBASELINE_NOTE,
    }


def format_win_rate_block(summary: dict[str, Any], *, label: str = "") -> list[str]:
    """Human-readable, side-by-side block shared by every script."""
    head = f"WIN RATE{f' ({label})' if label else ''}"
    return [
        head,
        f"  POSITION-level (TARGET, fee-adjusted): {summary['position_win_rate'] * 100:5.1f}%  "
        f"({summary['position_wins']}/{summary['position_n']} positions)",
        f"  ROW-level (legacy, inflated by partial TP legs): {summary['row_win_rate'] * 100:5.1f}%  "
        f"({summary['row_wins']}/{summary['row_n']} rows)",
        f"  excluded from position-level: {summary['excluded_bookkeeping_rows']} book-keeping rows, "
        f"{summary['excluded_open_positions']} still-open partial position(s); "
        f"fee-flipped winners: {summary['fee_flipped_positions']}",
        f"  Re-baseline note: {summary['note']}",
    ]


def win_rate_summary_from_storage(mode: Optional[str] = None, limit: int = 5000) -> dict[str, Any]:
    """Convenience wrapper over the app's own storage (import is lazy so this
    module stays importable without opening the DB)."""
    from app.storage import storage

    rows = [r for r in storage.closed_trades(limit=limit) if mode is None or r.get("mode") == mode]
    return win_rate_summary(rows, open_position_keys(storage.all_positions()))
