"""Tests for Stage 3 live vs backtest drift monitor (READ-ONLY, pure logic)."""
from __future__ import annotations

import json

import pytest

from app.research.drift import (
    BacktestBaseline,
    DriftThresholds,
    LiveSnapshot,
    avg_entry_spread_pct,
    compute_drift,
    default_baseline,
    exit_mix,
    load_baseline_from_json,
    pooled_max_drawdown_pct,
)


def _live(**kw) -> LiveSnapshot:
    base = dict(
        n_trades=40,
        expectancy_pct=0.015,
        expectancy_ci=(0.005, 0.025),
        win_rate=0.40,
        max_drawdown_pct=0.10,
        exit_reason_mix={"stop_loss": 0.6, "trailing_stop": 0.4},
        avg_entry_spread_pct=0.0020,
        modeled_fee_pct=0.0002,
        window_label="all history",
    )
    base.update(kw)
    return LiveSnapshot(**base)


def _bt(**kw) -> BacktestBaseline:
    base = dict(
        expectancy_pct=0.07,
        win_rate=0.65,
        max_drawdown_pct=0.15,
        modeled_slippage_pct=0.0010,
        exit_reason_mix={},
        source="test",
    )
    base.update(kw)
    return BacktestBaseline(**base)


# ── pure helpers ────────────────────────────────────────────────────────────

def test_pooled_max_drawdown_pct():
    # With full risk-per-trade (1.0) this compounds raw returns: 1.05 -> 1.0185
    # -> 1.03887 -> 0.99731; peak 1.05, trough 0.99731 -> ~5.02% drawdown.
    dd = pooled_max_drawdown_pct([0.05, -0.03, 0.02, -0.04], risk_per_trade_pct=1.0)
    assert dd == pytest.approx(0.0502, abs=1e-3)
    assert pooled_max_drawdown_pct([]) == 0.0
    assert pooled_max_drawdown_pct([0.01, 0.02]) == 0.0  # monotonic up
    # A single +94% outlier is tamed by fixed-risk scaling (no >100% drawdown).
    assert pooled_max_drawdown_pct([0.94, -0.02, -0.02]) < 0.02


def test_exit_mix():
    mix = exit_mix(["stop_loss", "stop_loss", "trailing_stop", ""])
    assert mix["stop_loss"] == pytest.approx(0.5)
    assert mix["trailing_stop"] == pytest.approx(0.25)
    assert mix["unknown"] == pytest.approx(0.25)
    assert exit_mix([]) == {}


def test_avg_entry_spread_pct_only_executed_buys():
    rows = [
        {"mode": "live", "action": "BUY", "executed": 1,
         "indicators": json.dumps({"spread_pct": 0.002})},
        {"mode": "live", "action": "BUY", "executed": 1,
         "indicators": json.dumps({"spread_pct": 0.004})},
        {"mode": "live", "action": "BUY", "executed": 0,  # not executed -> ignored
         "indicators": json.dumps({"spread_pct": 0.10})},
        {"mode": "live", "action": "HOLD", "executed": 1,  # not a BUY -> ignored
         "indicators": json.dumps({"spread_pct": 0.10})},
        {"mode": "paper", "action": "BUY", "executed": 1,  # wrong mode -> ignored
         "indicators": json.dumps({"spread_pct": 0.10})},
    ]
    assert avg_entry_spread_pct(rows, mode="live") == pytest.approx(0.003)
    assert avg_entry_spread_pct([]) is None


# ── drift computation ───────────────────────────────────────────────────────

def test_expectancy_drift_material_when_backtest_outside_ci():
    # backtest 7% is far outside live CI [0.5%, 2.5%] with adequate n -> material.
    report = compute_drift(_live(), _bt())
    exp = next(m for m in report.metrics if m.name == "expectancy_pct")
    assert exp.material is True
    assert report.strategy_drift is True


def test_expectancy_not_material_when_inside_ci():
    live = _live(expectancy_ci=(0.02, 0.12))  # backtest 7% inside CI
    report = compute_drift(live, _bt())
    exp = next(m for m in report.metrics if m.name == "expectancy_pct")
    assert exp.material is False


def test_small_sample_suppresses_strategy_flags():
    live = _live(n_trades=10)
    report = compute_drift(live, _bt())
    exp = next(m for m in report.metrics if m.name == "expectancy_pct")
    assert exp.material is False  # below min_trades
    assert any("small" in n for n in report.notes)


def test_execution_drift_flagged_when_spread_exceeds_modeled():
    live = _live(avg_entry_spread_pct=0.0040)  # modeled slippage 0.0010, delta 0.0030 > 0.0015
    report = compute_drift(live, _bt())
    ex = next(m for m in report.metrics if m.name == "entry_execution_cost")
    assert ex.material is True
    assert report.execution_drift is True


def test_execution_note_attributes_gap():
    live = _live(expectancy_pct=0.02, avg_entry_spread_pct=0.0040)
    report = compute_drift(live, _bt(expectancy_pct=0.07))
    assert any("execution cost" in n for n in report.notes)


def test_exit_mix_drift_only_when_baseline_supplies_mix():
    bt = _bt(exit_reason_mix={"stop_loss": 0.2, "trailing_stop": 0.5, "take_profit_1": 0.3})
    live = _live(exit_reason_mix={"stop_loss": 0.6, "trailing_stop": 0.4})
    report = compute_drift(live, bt)
    sl = next(m for m in report.metrics if m.name == "exit_mix:stop_loss")
    assert sl.delta == pytest.approx(0.4)  # 0.6 - 0.2
    assert sl.material is True  # 40pp > 15pp threshold


def test_default_baseline_has_no_exit_mix():
    b = default_baseline()
    assert b.exit_reason_mix == {}
    assert b.expectancy_pct > 0


def test_load_baseline_from_json(tmp_path):
    p = tmp_path / "bt.json"
    p.write_text(json.dumps({
        "expectancy_pct": 0.05, "win_rate": 0.6, "max_drawdown_pct": 0.12,
        "modeled_slippage_pct": 0.0012, "exit_reason_mix": {"stop_loss": 0.3},
        "source": "walkforward 2026", "n_trades": 120,
    }))
    b = load_baseline_from_json(p)
    assert b.expectancy_pct == 0.05
    assert b.exit_reason_mix == {"stop_loss": 0.3}
    assert b.n_trades == 120


def test_thresholds_are_configurable():
    live = _live(win_rate=0.50)  # 15pp below backtest 0.65
    strict = compute_drift(live, _bt(), DriftThresholds(win_rate_delta=0.10))
    lenient = compute_drift(live, _bt(), DriftThresholds(win_rate_delta=0.20))
    wr_strict = next(m for m in strict.metrics if m.name == "win_rate")
    wr_lenient = next(m for m in lenient.metrics if m.name == "win_rate")
    assert wr_strict.material is True
    assert wr_lenient.material is False
