from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from scripts.check_live_buy_readiness import readiness_blockers


def _settings(**overrides):
    values = {
        "live_mode": True,
        "paper_trading": False,
        "dry_run": False,
        "live_buys_enabled": True,
        "market_regime_gate_enabled": True,
        "breadth_gate_enabled": True,
        "profitstream_score_threshold": 80,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _safe_states():
    return {
        key: {"active": False}
        for key in (
            "baseline_unavailable", "drawdown_halt", "emergency_halt",
            "tick_protection", "circuit_breaker_check_failed",
            "exchange_filters_unavailable", "ml_gate_unavailable",
        )
    }


def _scan(**overrides):
    scan = {
        "timestamp": "2026-11-11T12:00:00+00:00",
        "actions": {"BUY": 1},
        "qualified_buy_candidates": 1,
        "highest_score": 90,
        "score_threshold": 80,
        "market_allowed": True,
        "breadth": {"allowed": True},
    }
    scan.update(overrides)
    return scan


def test_readiness_passes_all_live_gates():
    blockers = readiness_blockers(
        _settings(),
        {"running": True, "mode": "live"},
        {"entry_halted": False},
        _safe_states(),
        _scan(),
        now=datetime(2026, 11, 11, 12, 1, tzinfo=timezone.utc),
    )
    assert blockers == []


def test_readiness_reports_kill_switch_and_strategy_gates():
    blockers = readiness_blockers(
        _settings(live_buys_enabled=False),
        {"running": True, "mode": "live"},
        {"entry_halted": False},
        _safe_states(),
        _scan(actions={"HOLD": 44}, qualified_buy_candidates=0, highest_score=30,
              market_allowed=False, breadth={"allowed": False}),
        now=datetime(2026, 11, 11, 12, 1, tzinfo=timezone.utc),
    )
    assert "LIVE_BUYS_ENABLED is false" in blockers
    assert "latest ProfitStream scan produced no BUY actions" in blockers
    assert "highest scan score 30 is below threshold 80" in blockers
    assert "BTC market-regime gate is not allowing entries" in blockers
    assert "alt-market breadth gate is not allowing entries" in blockers
    assert "no BUY candidate passed the runner score threshold" in blockers


def test_readiness_reports_stale_scan_and_active_halt():
    states = _safe_states()
    states["emergency_halt"] = {"active": True, "reason": "test"}
    blockers = readiness_blockers(
        _settings(), {"running": True, "mode": "live"}, {"entry_halted": False},
        states, _scan(), now=datetime(2026, 11, 11, 12, 6, tzinfo=timezone.utc),
    )
    assert "emergency_halt is active: test" in blockers
    assert any(reason.startswith("ProfitStream scan is stale") for reason in blockers)