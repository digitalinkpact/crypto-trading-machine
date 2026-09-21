"""Stage 4 — Execution quality analysis (READ-ONLY, measurement only).

Measures the realized spread cost at entry per symbol (from ``tick_audit``) and
estimates what a passive limit / maker entry would have saved or cost — including
an estimated missed-fill rate from real candle data. It reports and recommends
only; it never changes the live order type.

Data note: ``orders.fee`` is not populated in this repo (fees are reconciled
into ``closed_trades``), and no per-order arrival/mid price is stored, so the
measurable execution cost at entry is the *spread* the taker crosses. The
modeled taker/maker fees come from ``binance_taker_fee`` / ``binance_maker_fee``.

Pure functions here take injected data (tick rows, a candle fetcher) so the
module is testable offline and imports no order path.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

import pandas as pd

CandleFetcher = Callable[[str], Optional[pd.DataFrame]]


# ── spread cost at entry (from executed-BUY tick_audit) ─────────────────────

@dataclass
class SymbolSpread:
    symbol: str
    n: int
    avg_spread_pct: float


@dataclass
class SpreadCosts:
    overall_avg_spread_pct: Optional[float]
    n_entries: int
    by_symbol: list[SymbolSpread] = field(default_factory=list)


def entry_spread_costs(tick_rows: list[dict], *, mode: str = "live") -> SpreadCosts:
    """Per-symbol and overall average entry spread from executed-BUY tick rows."""
    per: dict[str, list[float]] = {}
    for row in tick_rows:
        if str(row.get("mode") or "") != mode:
            continue
        if str(row.get("action") or "") != "BUY" or int(row.get("executed") or 0) != 1:
            continue
        try:
            ind = json.loads(row.get("indicators") or "{}")
        except (TypeError, ValueError):
            continue
        sp = ind.get("spread_pct") if isinstance(ind, dict) else None
        if isinstance(sp, (int, float)):
            per.setdefault(str(row.get("symbol") or ""), []).append(float(sp))

    by_symbol = [
        SymbolSpread(sym, len(v), sum(v) / len(v))
        for sym, v in per.items() if v
    ]
    by_symbol.sort(key=lambda s: -s.avg_spread_pct)
    all_spreads = [x for v in per.values() for x in v]
    overall = (sum(all_spreads) / len(all_spreads)) if all_spreads else None
    return SpreadCosts(overall_avg_spread_pct=overall, n_entries=len(all_spreads), by_symbol=by_symbol)


# ── maker vs taker entry economics ──────────────────────────────────────────

@dataclass
class MakerTakerEconomics:
    taker_entry_cost_pct: float
    maker_entry_cost_pct: float
    saving_if_filled_pct: float


def maker_taker_economics(
    avg_spread_pct: float, *, taker_fee: float, maker_fee: float
) -> MakerTakerEconomics:
    """Per-entry cost of a taker fill vs a maker (limit-at-bid) fill.

    Taker crosses half the spread and pays the taker fee; a maker posted at the
    bid earns half the spread and pays the maker fee. The saving from a *filled*
    maker entry is therefore ``(taker_fee - maker_fee) + spread``.
    """
    half = avg_spread_pct / 2.0
    taker_cost = taker_fee + half
    maker_cost = maker_fee - half
    return MakerTakerEconomics(
        taker_entry_cost_pct=taker_cost,
        maker_entry_cost_pct=maker_cost,
        saving_if_filled_pct=taker_cost - maker_cost,
    )


# ── missed-fill estimate from real candles ──────────────────────────────────

@dataclass
class FillEstimate:
    n: int
    filled: int
    fill_rate: Optional[float]
    missed_rate: Optional[float]


def estimate_limit_fill_rate(
    entries: list[tuple[str, datetime]],
    fetch_candles: CandleFetcher,
    *,
    offset_pct: float,
) -> FillEstimate:
    """Estimate how often a passive limit posted ``offset_pct`` below the entry
    bar's open would have filled, using real daily candles.

    A maker BUY posted at ``open * (1 - offset_pct)`` (≈ the bid) fills within
    the entry bar iff that bar's low reached the limit. ``offset_pct`` is
    typically half the average spread. Candles are cached per symbol.
    """
    cache: dict[str, Optional[pd.DataFrame]] = {}
    n = 0
    filled = 0
    for symbol, signal_ts in entries:
        if symbol not in cache:
            cache[symbol] = fetch_candles(symbol)
        candles = cache[symbol]
        if candles is None or candles.empty or "open_time" not in candles.columns:
            continue
        future = candles[candles["open_time"] > signal_ts]
        if future.empty:
            continue
        bar = future.iloc[0]
        try:
            open_px = float(bar["open"])
            low = float(bar["low"])
        except (TypeError, ValueError):
            continue
        if open_px <= 0:
            continue
        n += 1
        limit = open_px * (1.0 - offset_pct)
        if low <= limit:
            filled += 1
    if n == 0:
        return FillEstimate(0, 0, None, None)
    fill_rate = filled / n
    return FillEstimate(n, filled, fill_rate, 1.0 - fill_rate)


# ── expected benefit of switching entries to maker limits ───────────────────

def expected_entry_benefit(
    *, saving_if_filled_pct: float, fill_rate: float, forgone_edge_per_miss_pct: float
) -> float:
    """Expected per-opportunity change vs always-taker, if entries were maker
    limits: gain the fill saving when filled, forgo the trade's edge when missed.

    ``forgone_edge_per_miss_pct`` is the live net expectancy per trade — if it
    is negative (the strategy loses per trade), missing entries is itself a
    benefit, which this surfaces honestly.
    """
    return fill_rate * saving_if_filled_pct - (1.0 - fill_rate) * forgone_edge_per_miss_pct


def breakeven_fill_rate(*, saving_if_filled_pct: float, forgone_edge_per_miss_pct: float) -> Optional[float]:
    """Fill rate at which maker entries break even vs taker. None when the
    forgone edge is <= 0 (missing trades never hurts, so any fill rate wins)."""
    denom = saving_if_filled_pct + forgone_edge_per_miss_pct
    if forgone_edge_per_miss_pct <= 0 or denom <= 0:
        return None
    return forgone_edge_per_miss_pct / denom


def collect_buy_entries(tick_rows: list[dict], *, mode: str = "live") -> list[tuple[str, datetime]]:
    """(symbol, signal_ts) for every executed-BUY tick row — the entry population
    for the fill-rate estimate."""
    out: list[tuple[str, datetime]] = []
    for row in tick_rows:
        if str(row.get("mode") or "") != mode:
            continue
        if str(row.get("action") or "") != "BUY" or int(row.get("executed") or 0) != 1:
            continue
        try:
            ts = datetime.fromisoformat(str(row.get("ts")))
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        out.append((str(row.get("symbol") or ""), ts))
    return out
