"""Tests for Stage 4 execution-quality analysis + shared research helpers."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.research.execution_quality import (
    breakeven_fill_rate,
    collect_buy_entries,
    entry_spread_costs,
    estimate_limit_fill_rate,
    expected_entry_benefit,
    maker_taker_economics,
)
from app.research.rejected_setups import summarize_by_reason


def _tick(symbol, spread, *, executed=1, action="BUY", mode="live",
          ts="2025-01-01T00:00:00+00:00"):
    return {
        "mode": mode, "symbol": symbol, "action": action, "executed": executed, "ts": ts,
        "indicators": json.dumps({"spread_pct": spread}),
    }


# ── spread costs ────────────────────────────────────────────────────────────

def test_entry_spread_costs_by_symbol():
    rows = [
        _tick("AUSDT", 0.002), _tick("AUSDT", 0.004),
        _tick("BUSDT", 0.010),
        _tick("CUSDT", 0.001, executed=0),   # ignored (not executed)
        _tick("DUSDT", 0.001, action="SELL"),  # ignored (not a BUY)
    ]
    costs = entry_spread_costs(rows, mode="live")
    assert costs.n_entries == 3
    assert costs.overall_avg_spread_pct == pytest.approx((0.002 + 0.004 + 0.010) / 3)
    # Worst-spread symbol first.
    assert costs.by_symbol[0].symbol == "BUSDT"
    assert costs.by_symbol[0].avg_spread_pct == pytest.approx(0.010)


# ── maker/taker economics ───────────────────────────────────────────────────

def test_maker_taker_economics():
    econ = maker_taker_economics(0.004, taker_fee=0.002, maker_fee=0.001)
    assert econ.taker_entry_cost_pct == pytest.approx(0.002 + 0.002)   # fee + half-spread
    assert econ.maker_entry_cost_pct == pytest.approx(0.001 - 0.002)   # fee - half-spread
    # saving = (taker_fee - maker_fee) + spread
    assert econ.saving_if_filled_pct == pytest.approx((0.002 - 0.001) + 0.004)


# ── fill-rate estimate ──────────────────────────────────────────────────────

def _candles(bars, start):
    rows = []
    for i, (o, h, l, c) in enumerate(bars):
        rows.append({"open_time": pd.Timestamp(start + timedelta(days=i)),
                     "open": o, "high": h, "low": l, "close": c})
    return pd.DataFrame(rows)


def test_estimate_limit_fill_rate():
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    # entry bar open 100; limit at 1% below = 99.
    fills = _candles([(100, 101, 98, 100)], start)      # low 98 <= 99 -> filled
    misses = _candles([(100, 101, 99.5, 100)], start)   # low 99.5 > 99 -> missed

    def fetch(sym):
        return {"F": fills, "M": misses}.get(sym)

    sig = datetime(2024, 12, 31, tzinfo=timezone.utc)
    est = estimate_limit_fill_rate([("F", sig), ("M", sig)], fetch, offset_pct=0.01)
    assert est.n == 2
    assert est.filled == 1
    assert est.fill_rate == pytest.approx(0.5)
    assert est.missed_rate == pytest.approx(0.5)


def test_estimate_limit_fill_rate_no_data():
    est = estimate_limit_fill_rate([("X", datetime.now(timezone.utc))],
                                   lambda s: None, offset_pct=0.01)
    assert est.n == 0 and est.fill_rate is None


# ── benefit + breakeven ─────────────────────────────────────────────────────

def test_expected_entry_benefit_and_breakeven():
    # saving 0.5% per fill, forgone edge 1% per miss.
    b_high_fill = expected_entry_benefit(saving_if_filled_pct=0.005, fill_rate=0.9,
                                         forgone_edge_per_miss_pct=0.01)
    b_low_fill = expected_entry_benefit(saving_if_filled_pct=0.005, fill_rate=0.5,
                                        forgone_edge_per_miss_pct=0.01)
    assert b_high_fill > b_low_fill
    be = breakeven_fill_rate(saving_if_filled_pct=0.005, forgone_edge_per_miss_pct=0.01)
    assert be == pytest.approx(0.01 / (0.005 + 0.01))


def test_breakeven_none_when_negative_edge():
    # If the strategy loses money per trade, missing trades never hurts.
    assert breakeven_fill_rate(saving_if_filled_pct=0.005,
                               forgone_edge_per_miss_pct=-0.01) is None


def test_collect_buy_entries_filters():
    rows = [
        _tick("AUSDT", 0.002, ts="2025-01-01T00:00:00+00:00"),
        _tick("BUSDT", 0.002, executed=0),
        _tick("CUSDT", 0.002, mode="paper"),
    ]
    entries = collect_buy_entries(rows, mode="live")
    assert [e[0] for e in entries] == ["AUSDT"]
    assert entries[0][1].tzinfo is not None


# ── shared rejected-setup summary helper ────────────────────────────────────

def test_summarize_by_reason():
    outcomes = [
        {"sim_pnl_pct": 0.05, "reject_reasons": ["spread"]},
        {"sim_pnl_pct": -0.03, "reject_reasons": ["spread", "score_threshold"]},
        {"sim_pnl_pct": 0.02, "reject_reasons": ["spread"]},
    ]
    rows = summarize_by_reason(outcomes, min_samples=2)
    by = {r["reason"]: r for r in rows}
    assert by["spread"]["n"] == 3
    assert by["spread"]["winners"] == 2
    assert by["spread"]["sufficient"] is True
    assert by["score_threshold"]["n"] == 1
    assert by["score_threshold"]["sufficient"] is False
    # Sorted by sample size descending.
    assert rows[0]["reason"] == "spread"
