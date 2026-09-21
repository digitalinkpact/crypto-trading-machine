"""Tests for Stage 5 — weekly experiment verdict card (READ-ONLY)."""
from types import SimpleNamespace

from app.research.experiment_verdict import (
    KEEP_TESTING,
    PAUSE,
    PROMOTE,
    REJECT,
    unify_verdict,
)


def _drift(*, exec_material=False, dd_material=False, strategy=False, execution=False):
    metrics = [
        SimpleNamespace(
            name="entry_execution_cost", kind="execution",
            live=0.006, backtest=0.001, delta=0.005,
            material=exec_material, detail="live spread 60bps vs modeled 10bps",
        ),
        SimpleNamespace(
            name="max_drawdown_pct", kind="strategy",
            live=0.20, backtest=0.10, delta=0.10,
            material=dd_material, detail="live 20% vs backtest 10%",
        ),
    ]
    return SimpleNamespace(
        metrics=metrics,
        strategy_drift=strategy,
        execution_drift=execution,
        n_live_trades=50,
        notes=[],
    )


def _promotion(verdict: str, *, all_passed: bool = False):
    criteria = [
        SimpleNamespace(name="sample_size", passed=all_passed, detail="30 trades"),
        SimpleNamespace(name="positive_edge", passed=all_passed, detail="ci lo > 0"),
    ]
    return SimpleNamespace(verdict=verdict, criteria=criteria)


def test_execution_drift_forces_pause():
    card = unify_verdict(
        stage2_verdict=_promotion("PROMOTE", all_passed=True),
        stage3_drift=_drift(exec_material=True, execution=True),
    )
    assert card.overall == PAUSE
    assert any("execution" in r.lower() for r in card.reasons)
    assert any("passive limit" in r.lower() for r in card.recommendations)


def test_drawdown_drift_forces_pause_even_over_promote():
    card = unify_verdict(
        stage2_verdict=_promotion("PROMOTE", all_passed=True),
        stage3_drift=_drift(dd_material=True, strategy=True),
    )
    assert card.overall == PAUSE
    assert any("drawdown" in r.lower() for r in card.reasons)


def test_stage2_reject_flows_through():
    card = unify_verdict(
        stage2_verdict=_promotion("REJECT"),
        stage3_drift=_drift(),  # no material metrics
    )
    assert card.overall == REJECT
    assert any("stop" in r.lower() for r in card.recommendations)


def test_promote_only_when_no_drift():
    card = unify_verdict(
        stage2_verdict=_promotion("PROMOTE", all_passed=True),
        stage3_drift=_drift(),  # no drift
    )
    assert card.overall == PROMOTE


def test_promote_downgraded_when_any_drift_flag_is_set():
    card = unify_verdict(
        stage2_verdict=_promotion("PROMOTE", all_passed=True),
        stage3_drift=_drift(strategy=True),  # strategy_drift=True but no material metric
    )
    assert card.overall == KEEP_TESTING


def test_missing_inputs_default_to_keep_testing():
    card = unify_verdict()
    assert card.overall == KEEP_TESTING
    assert card.inputs_present == {
        "stage1_rejected_setups": False,
        "stage2_promotion": False,
        "stage3_drift": False,
        "stage4_execution": False,
    }


def test_stage1_high_opportunity_cost_becomes_concern():
    summary = [
        {"reason": "spread_gate", "opportunity_cost_pct": 0.012},
        {"reason": "min_conf", "opportunity_cost_pct": 0.001},  # below threshold
    ]
    card = unify_verdict(
        stage1_summary=summary,
        stage2_verdict=_promotion("KEEP TESTING"),
    )
    assert any("spread_gate" in c for c in card.concerns)
    assert not any("min_conf" in c for c in card.concerns)


def test_stage4_wide_spread_becomes_concern():
    spread = SimpleNamespace(overall_avg_spread_pct=0.004, n_entries=100, by_symbol=[])
    card = unify_verdict(
        stage2_verdict=_promotion("KEEP TESTING"),
        stage4_spread=spread,
        stage4_maker_benefit_pct=0.002,
    )
    assert any("spread" in c.lower() for c in card.concerns)
    assert any("maker" in c.lower() for c in card.concerns)


def test_to_dict_serializes_all_fields():
    card = unify_verdict(stage2_verdict=_promotion("REJECT"))
    d = card.to_dict()
    assert set(d.keys()) == {"overall", "reasons", "concerns", "recommendations", "inputs_present"}
