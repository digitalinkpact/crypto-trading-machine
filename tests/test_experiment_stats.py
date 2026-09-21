"""Tests for Stage 2 live-trade statistics + promotion criteria (READ-ONLY)."""
from __future__ import annotations

import pytest

from app.trading.experiment_stats import (
    build_report,
    compute_stats,
    concentration,
    evaluate_promotion,
    observed_regimes,
    regime_label,
    trade_pnl,
    trade_pnl_pct,
)


def _t(pnl, pnl_pct, *, symbol="AUSDT", regime=1, exit_reason="take_profit_1",
       exit_ts="2025-01-01T00:00:00+00:00", **kw):
    d = dict(
        mode="live", symbol=symbol, pnl=pnl, pnl_pct=pnl_pct,
        entry_btc_regime=regime, exit_reason=exit_reason, exit_ts=exit_ts, mfe_pct=None,
    )
    d.update(kw)
    return d


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def closed_trades(self, limit: int = 100):
        return list(self._rows)


# ── helpers ─────────────────────────────────────────────────────────────────

def test_regime_label_mapping():
    assert regime_label(2) == "STRONG_BULL"
    assert regime_label(1) == "BULL"
    assert regime_label(0) == "SIDEWAYS"
    assert regime_label(-1) == "BEAR"
    assert regime_label(-2) == "STRONG_BEAR"
    assert regime_label(None) == "unknown"


def test_trade_pnl_prefers_corrected():
    row = _t(10, 5, pnl_corrected=8, pnl_pct_corrected=4)
    assert trade_pnl(row) == 8.0
    assert trade_pnl_pct(row) == pytest.approx(0.04)
    # Falls back to raw when corrected is absent.
    assert trade_pnl(_t(10, 5)) == 10.0
    assert trade_pnl_pct(_t(10, 5)) == pytest.approx(0.05)


def test_observed_regimes():
    trades = [_t(1, 1, regime=1), _t(1, 1, regime=0), _t(1, 1, regime=None)]
    assert observed_regimes(trades) == {"BULL", "SIDEWAYS"}
    assert observed_regimes([_t(1, 1, regime=None)]) == set()


def test_concentration():
    trades = [
        _t(10, 5, symbol="AUSDT"),
        _t(5, 5, symbol="AUSDT"),
        _t(5, 5, symbol="BUSDT"),
        _t(-3, -3, symbol="CUSDT"),
    ]
    c = concentration(trades)
    assert c.gross_profit_usd == pytest.approx(20.0)
    assert c.max_trade_frac == pytest.approx(0.5)
    assert c.max_symbol_frac == pytest.approx(0.75)
    assert c.top_symbol == "AUSDT"


# ── stats ───────────────────────────────────────────────────────────────────

def test_compute_stats_basic():
    trades = [
        _t(10, 5, exit_ts="2025-01-01T00:00:00+00:00"),
        _t(-4, -2, exit_ts="2025-01-02T00:00:00+00:00", exit_reason="stop_loss"),
    ]
    s = compute_stats(trades)
    assert s.n == 2
    assert s.wins == 1 and s.losses == 1
    assert s.win_rate == pytest.approx(0.5)
    assert s.gross_profit_usd == pytest.approx(10.0)
    assert s.gross_loss_usd == pytest.approx(4.0)
    assert s.profit_factor == pytest.approx(2.5)
    assert s.expectancy_usd == pytest.approx(3.0)
    assert s.net_pnl_usd == pytest.approx(6.0)
    assert s.max_drawdown_usd == pytest.approx(4.0)  # win then loss: peak 10 -> 6
    assert s.max_losing_streak == 1


def test_compute_stats_empty():
    s = compute_stats([])
    assert s.n == 0
    assert s.profit_factor == 0.0


# ── promotion verdicts ──────────────────────────────────────────────────────

def _promote_set() -> list[dict]:
    trades = []
    symbols = [f"S{i}USDT" for i in range(10)]
    for i in range(28):
        trades.append(_t(3.0, 3.0, symbol=symbols[i % 10],
                         regime=1 if i % 2 == 0 else 0,
                         exit_ts=f"2025-02-{(i % 27) + 1:02d}T00:00:00+00:00"))
    for i in range(2):
        trades.append(_t(-0.5, -0.5, symbol=symbols[i], regime=0,
                        exit_reason="stop_loss",
                        exit_ts=f"2025-03-{i + 1:02d}T00:00:00+00:00"))
    return trades


def test_evaluate_promotion_promote():
    v = evaluate_promotion(_promote_set())
    assert v.verdict == "PROMOTE"
    assert all(c.passed is True for c in v.criteria)


def test_evaluate_promotion_reject_on_no_edge():
    trades = []
    for i in range(10):
        trades.append(_t(1.0, 1.0, symbol="AUSDT", regime=1,
                        exit_ts=f"2025-02-{i + 1:02d}T00:00:00+00:00"))
    for i in range(30):
        trades.append(_t(-2.0, -2.0, symbol="AUSDT", regime=0,
                        exit_reason="stop_loss",
                        exit_ts=f"2025-03-{i + 1:02d}T00:00:00+00:00"))
    v = evaluate_promotion(trades)
    assert v.verdict == "REJECT"
    assert v.stats.profit_factor < 1.0


def test_evaluate_promotion_keep_testing_when_too_few_trades():
    trades = [_t(2.0, 2.0, regime=1, exit_ts=f"2025-02-{i + 1:02d}T00:00:00+00:00")
              for i in range(10)]
    v = evaluate_promotion(trades)
    assert v.verdict == "KEEP TESTING"
    sample = next(c for c in v.criteria if c.name == "sample_size")
    assert sample.passed is False


# ── report orchestration ────────────────────────────────────────────────────

def test_build_report_splits_and_verdict():
    store = _FakeStore([
        _t(3, 3, symbol="AUSDT", regime=1, exit_reason="take_profit_1"),
        _t(-1, -1, symbol="BUSDT", regime=0, exit_reason="stop_loss"),
        _t(2, 2, symbol="AUSDT", regime=1, exit_reason="trailing_stop"),
        # A paper trade that must be excluded from a live report.
        dict(_t(99, 99, symbol="ZUSDT", regime=1), mode="paper"),
    ])
    report = build_report(mode="live", source_storage=store)
    assert report["overall"].n == 3
    assert set(report["by_regime"]) == {"BULL", "SIDEWAYS"}
    assert set(report["by_exit_reason"]) == {"take_profit_1", "stop_loss", "trailing_stop"}
    assert report["verdict"].verdict in {"PROMOTE", "KEEP TESTING", "REJECT"}
