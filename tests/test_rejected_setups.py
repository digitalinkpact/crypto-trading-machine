"""Tests for the Stage 1 rejected-setup outcome tracking (READ-ONLY research).

All tests are offline: candle data is synthetic and injected, and results are
written to a temp research database — never the live trading tables.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.research.bootstrap import bootstrap_ci, expectancy, profit_factor, win_rate
from app.research.rejected_setups import (
    LadderParams,
    ResearchStore,
    build_rejected_setup_outcomes,
    classify_rejection,
    simulate_exit,
)
from app.storage.db import Storage


# ── helpers ─────────────────────────────────────────────────────────────────

def _params(**overrides) -> LadderParams:
    base = dict(
        stop_loss_pct=0.05,
        atr_stop_enabled=False,
        atr_stop_multiple=2.0,
        atr_stop_min_pct=0.02,
        atr_stop_max_pct=0.08,
        tp1_pct=0.08,
        tp1_frac=0.50,
        tp2_pct=0.15,
        tp2_frac=0.25,
        trail_activation=0.05,
        trail_distance=0.03,
        trailing_requires_tp1=False,
        stale_enabled=False,
        stale_hours=48.0,
        stale_max_pnl=0.02,
        max_hold_hours=96.0,
        fee_rate=0.0,
        slippage_pct=0.0,
    )
    base.update(overrides)
    return LadderParams(**base)


def _candles(bars, *, start: datetime | None = None) -> pd.DataFrame:
    """bars: list of (open, high, low, close), one daily candle each."""
    start = start or datetime(2025, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i, (o, h, l, c) in enumerate(bars):
        rows.append(
            {
                "open_time": pd.Timestamp(start + timedelta(days=i)),
                "open": float(o),
                "high": float(h),
                "low": float(l),
                "close": float(c),
            }
        )
    return pd.DataFrame(rows)


def _row(**kw) -> dict:
    """Build a tick_audit-shaped row dict (indicators/filters as JSON strings)."""
    ind = kw.pop("indicators", {})
    filt = kw.pop("filters", {})
    base = dict(
        id=1,
        ts="2025-01-01T00:00:00+00:00",
        mode="live",
        symbol="ABCUSDT",
        timeframe="1m/5m/15m/1h",
        action="HOLD",
        score=60,
        executed=0,
        reason="",
    )
    base.update(kw)
    base["indicators"] = json.dumps(ind)
    base["filters"] = json.dumps(filt)
    return base


# ── bootstrap helpers ───────────────────────────────────────────────────────

def test_bootstrap_ci_brackets_mean():
    values = [0.01, -0.02, 0.03, 0.05, -0.01, 0.02]
    lo, hi = bootstrap_ci(values, n_resamples=500)
    assert lo <= expectancy(values) <= hi


def test_bootstrap_ci_edge_cases():
    assert bootstrap_ci([]) != bootstrap_ci([])  # (nan, nan) — nan != nan
    assert bootstrap_ci([0.5]) == (0.5, 0.5)


def test_profit_factor_and_win_rate():
    assert win_rate([0.1, -0.1, 0.2, 0.0]) == pytest.approx(0.5)
    assert profit_factor([0.3, -0.1]) == pytest.approx(3.0)
    assert profit_factor([0.3, 0.1]) == float("inf")
    assert profit_factor([-0.3, -0.1]) == 0.0


# ── classification ──────────────────────────────────────────────────────────

def test_classify_ready_dip_blocked_by_spread():
    row = _row(
        reason="spread_wide:0.4000%>0.2500%; btc_trend_not_aligned_soft",
        indicators={"rsi_1d": 25.0, "bb_lower_1d": 1.0, "entry_strategy": "dip_buy",
                    "btc_regime_label": "BULL"},
        filters={"score_threshold": 80},
        score=90,
    )
    rs = classify_rejection(row)
    assert rs is not None
    assert "spread" in rs.reject_reasons
    assert "score_threshold" not in rs.reject_reasons  # a hard filter blocked it
    assert rs.entry_type == "dip_buy"
    assert rs.btc_regime_label == "BULL"


def test_classify_score_threshold_derived():
    row = _row(
        reason="btc_trend_not_aligned_soft",
        indicators={"pullback_ready": True, "rsi_1d": 55.0, "bb_lower_1d": 1.0},
        filters={"score_threshold": 80},
        score=60,
        action="HOLD",
    )
    rs = classify_rejection(row)
    assert rs is not None
    assert rs.reject_reasons == ["score_threshold"]
    assert rs.entry_type == "pullback"


def test_classify_regime_gate_hard():
    row = _row(
        reason="market_gate: BTC bear",
        indicators={"pullback_ready": True, "rsi_1d": 55.0, "bb_lower_1d": 1.0},
        filters={"score_threshold": 80},
    )
    rs = classify_rejection(row)
    assert rs is not None
    assert "regime_gate" in rs.reject_reasons


def test_classify_excludes_executed_held_and_no_setup():
    executed = _row(executed=1, action="BUY",
                    indicators={"pullback_ready": True, "rsi_1d": 55.0, "bb_lower_1d": 1.0})
    held = _row(reason="position_already_open",
                indicators={"pullback_ready": True, "rsi_1d": 55.0, "bb_lower_1d": 1.0})
    no_setup = _row(reason="rsi_not_oversold; close_above_lower_band",
                    indicators={"rsi_1d": 55.0, "bb_lower_1d": 1.0, "decision": "hold"})
    insufficient = _row(reason="insufficient_history", indicators={})
    assert classify_rejection(executed) is None
    assert classify_rejection(held) is None
    assert classify_rejection(no_setup) is None
    assert classify_rejection(insufficient) is None


# ── simulation ──────────────────────────────────────────────────────────────

def test_simulate_stop_first_when_both_touched():
    # Bar 0 (entry): open 100, high 110 (>=TP1 108), low 94 (<=stop 95). Stop wins.
    candles = _candles([(100, 110, 94, 100), (100, 100, 100, 100)])
    sig = datetime(2024, 12, 31, tzinfo=timezone.utc)
    res = simulate_exit(candles, sig, None, _params())
    assert res is not None
    assert res.exit_reason == "stop_loss"
    assert res.exit_price == pytest.approx(95.0)
    assert res.pnl_pct == pytest.approx(-0.05, abs=1e-9)


def test_simulate_tp_scaleout_then_trailing():
    candles = _candles([
        (100, 109, 100, 108),   # TP1 at 108 (50%)
        (108, 116, 109, 115),   # TP2 at 115 (25%)
        (115, 117, 112, 113),   # trailing stop on the remainder
    ])
    sig = datetime(2024, 12, 31, tzinfo=timezone.utc)
    res = simulate_exit(candles, sig, None, _params())
    assert res is not None
    assert res.exit_reason == "trailing_stop"
    assert res.n_legs == 3
    assert res.pnl_pct > 0.05
    assert res.mfe_pct >= 0.15


def test_simulate_max_hold_exit():
    bars = [(100, 100.5, 99.5, 100)] * 6  # flat, no stop/tp; stale disabled
    candles = _candles(bars)
    sig = datetime(2024, 12, 31, tzinfo=timezone.utc)
    res = simulate_exit(candles, sig, None, _params(max_hold_hours=96.0))
    assert res is not None
    assert res.exit_reason == "max_hold"
    assert abs(res.pnl_pct) < 0.01


def test_simulate_stale_dead_money_exit():
    bars = [(100, 100.5, 99.5, 100)] * 6
    candles = _candles(bars)
    sig = datetime(2024, 12, 31, tzinfo=timezone.utc)
    res = simulate_exit(candles, sig, None, _params(stale_enabled=True, stale_hours=48.0))
    assert res is not None
    assert res.exit_reason == "stale_dead_money"


def test_simulate_fees_and_slippage_reduce_pnl():
    candles = _candles([
        (100, 109, 100, 108),
        (108, 116, 109, 115),
        (115, 117, 112, 113),
    ])
    sig = datetime(2024, 12, 31, tzinfo=timezone.utc)
    clean = simulate_exit(candles, sig, None, _params())
    costed = simulate_exit(candles, sig, None, _params(fee_rate=0.001, slippage_pct=0.001))
    assert clean is not None and costed is not None
    assert costed.pnl_pct < clean.pnl_pct


def test_simulate_returns_none_without_forward_data():
    candles = _candles([(100, 101, 99, 100)])
    sig = datetime(2025, 6, 1, tzinfo=timezone.utc)  # after all candles
    assert simulate_exit(candles, sig, None, _params()) is None


def test_atr_scaled_stop_used_when_enabled():
    # atr_pct 0.04 * multiple 2 = 0.08 stop (clamped within [0.03, 0.08]).
    p = _params(atr_stop_enabled=True, atr_stop_multiple=2.0)
    candles = _candles([(100, 101, 91.5, 100), (100, 100, 100, 100)])  # low 91.5 <= stop 92
    sig = datetime(2024, 12, 31, tzinfo=timezone.utc)
    res = simulate_exit(candles, sig, 0.04, p)
    assert res is not None
    assert res.exit_reason == "stop_loss"
    assert res.exit_price == pytest.approx(92.0)


# ── storage + orchestration ─────────────────────────────────────────────────

def test_research_store_isolated_roundtrip(tmp_path):
    store = ResearchStore(tmp_path / "research.db")
    assert store.existing_tick_ids() == set()
    assert store.all_outcomes() == []


def test_build_end_to_end_and_idempotent(tmp_path):
    src = Storage(path=tmp_path / "trading.db")
    # A ready dip setup blocked by spread (should be simulated) ...
    src.record_tick_audit(
        mode="paper", symbol="ABCUSDT", timeframe="1m/5m/15m/1h", action="HOLD",
        score=90, executed=False,
        reason="spread_wide:0.4000%>0.2500%; btc_trend_not_aligned_soft",
        indicators={"rsi_1d": 25.0, "bb_lower_1d": 1.0, "entry_strategy": "dip_buy",
                    "btc_regime_label": "BULL"},
        filters={"score_threshold": 80},
    )
    # ... and a no-setup tick (should be ignored).
    src.record_tick_audit(
        mode="paper", symbol="ABCUSDT", timeframe="1m/5m/15m/1h", action="HOLD",
        score=10, executed=False,
        reason="rsi_not_oversold; close_above_lower_band",
        indicators={"rsi_1d": 60.0, "bb_lower_1d": 1.0, "decision": "hold"},
        filters={"score_threshold": 80},
    )

    start = datetime.now(timezone.utc) + timedelta(days=1)
    candles = _candles([(100, 109, 100, 108), (108, 116, 109, 115),
                        (115, 117, 112, 113)], start=start)

    def fake_fetch(symbol: str):
        return candles if symbol == "ABCUSDT" else None

    db = tmp_path / "research.db"
    summary = build_rejected_setup_outcomes(
        mode="paper", db_path=db, source_storage=src, fetch_candles=fake_fetch,
    )
    assert summary.rejected_setups == 1
    assert summary.stored == 1

    outcomes = ResearchStore(db).all_outcomes()
    assert len(outcomes) == 1
    assert outcomes[0]["symbol"] == "ABCUSDT"
    assert "spread" in outcomes[0]["reject_reasons"]
    assert outcomes[0]["sim_pnl_pct"] > 0

    # Second run is idempotent — nothing new stored.
    again = build_rejected_setup_outcomes(
        mode="paper", db_path=db, source_storage=src, fetch_candles=fake_fetch,
    )
    assert again.stored == 0
    assert again.skipped_existing == 1
    assert len(ResearchStore(db).all_outcomes()) == 1
