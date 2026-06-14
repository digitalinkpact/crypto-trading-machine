"""Tests for the ML quality gate inference path in the autopilot.

These exercise `Autopilot._ml_win_proba` and the gate decision without touching
the network, disk cache, or placing trades. A tiny real sklearn pipeline is
trained so the 7-feature contract with `app.regime.trainer._rows_to_xy` is
enforced (feature count / order mismatches will surface here).
"""
from __future__ import annotations

import numpy as np
import pytest
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.config import Timeframe
from app.regime.trainer import _rows_to_xy
from app.signals import Signal, SignalAction
from app.trading.autopilot import Autopilot

# Feature order the gate must produce, mirrored from trainer._rows_to_xy:
# [confidence, atr_pct, rsi_14, ema_gap_pct, agent_count, tf_weight, action]
N_FEATURES = 7


def _toy_model() -> Pipeline:
    """A deterministic 7-feature classifier.

    Trains so that higher confidence + positive ema_gap → win. Guarantees a
    valid predict_proba and locks in the expected feature count.
    """
    rng = np.random.default_rng(0)
    rows = []
    for _ in range(200):
        action = "BUY" if rng.random() > 0.5 else "SELL"
        conf = float(rng.uniform(0.4, 0.95))
        ema_gap = float(rng.uniform(-0.05, 0.05))
        rows.append({
            "action": action,
            "timeframe": "1d",
            "confidence": conf,
            "atr_pct": float(rng.uniform(0.005, 0.05)),
            "rsi_14": float(rng.uniform(20, 80)),
            "ema_gap_pct": ema_gap,
            "agent_count": int(rng.integers(1, 4)),
            # Win when the signal is confident and trend-aligned.
            "outcome_win": 1 if (conf > 0.6 and ema_gap > 0) else 0,
        })
    x, y = _rows_to_xy(rows)
    assert x.shape[1] == N_FEATURES
    model = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", LogisticRegression(max_iter=1000, class_weight="balanced")),
    ])
    model.fit(x, y)
    return model


def _signal(action: SignalAction = SignalAction.BUY, conf: float = 0.7) -> Signal:
    return Signal(
        agent="test",
        symbol="BTCUSDT",
        timeframe=Timeframe.D1,
        action=action,
        confidence=conf,
        contributing_agents=("a", "b"),
    )


@pytest.fixture
def autopilot(monkeypatch):
    ap = Autopilot()
    # Avoid network/disk: return a fixed daily feature snapshot.
    async def _fake_snapshot(_self, _symbol):
        return 0.02, 55.0, 0.01  # atr_pct, rsi_14, ema_gap_pct
    monkeypatch.setattr(Autopilot, "_feature_snapshot", _fake_snapshot, raising=True)
    return ap


async def test_proba_is_valid_probability(autopilot):
    model = _toy_model()
    proba = await autopilot._ml_win_proba(model, "BTCUSDT", _signal())
    assert proba is not None
    assert 0.0 <= proba <= 1.0


async def test_feature_vector_matches_trainer_contract(autopilot):
    """The gate must build exactly N_FEATURES; a wrong count raises in predict."""
    model = _toy_model()
    # A model expecting the right number of features should not raise.
    proba = await autopilot._ml_win_proba(model, "BTCUSDT", _signal())
    assert proba is not None


async def test_high_conf_uptrend_scores_higher_than_low_conf(autopilot):
    model = _toy_model()
    strong = await autopilot._ml_win_proba(
        model, "BTCUSDT", _signal(SignalAction.BUY, conf=0.9)
    )
    weak = await autopilot._ml_win_proba(
        model, "BTCUSDT", _signal(SignalAction.BUY, conf=0.45)
    )
    assert strong > weak


async def test_fail_open_when_feature_snapshot_raises(monkeypatch):
    """If feature extraction throws, proba is None so the caller lets the trade through."""
    ap = Autopilot()

    async def _boom(_self, _symbol):
        raise RuntimeError("ohlcv unavailable")

    monkeypatch.setattr(Autopilot, "_feature_snapshot", _boom, raising=True)
    proba = await ap._ml_win_proba(_toy_model(), "BTCUSDT", _signal())
    assert proba is None


async def test_fail_open_when_model_predict_raises(autopilot):
    """A broken model must not crash the tick — proba returns None (fail-open)."""
    class _BadModel:
        def predict_proba(self, _x):
            raise ValueError("not fitted")

    proba = await autopilot._ml_win_proba(_BadModel(), "BTCUSDT", _signal())
    assert proba is None


# ── staleness guard ─────────────────────────────────────────────────────
from datetime import datetime, timedelta, timezone  # noqa: E402

from app.trading.autopilot import _model_age_hours  # noqa: E402


def test_model_age_hours_recent():
    ts = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    age = _model_age_hours(ts)
    assert age is not None
    assert 4.9 < age < 5.1


def test_model_age_hours_stale():
    ts = (datetime.now(timezone.utc) - timedelta(days=17)).isoformat()
    age = _model_age_hours(ts)
    assert age is not None
    assert age > 72  # trips the default fail-open window


def test_model_age_hours_naive_timestamp_treated_as_utc():
    # Persisted timestamps may lack tzinfo; must not raise and must compute.
    ts = (
        (datetime.now(timezone.utc) - timedelta(hours=10))
        .replace(tzinfo=None)
        .isoformat()
    )
    age = _model_age_hours(ts)
    assert age is not None
    assert 9.5 < age < 10.5


def test_model_age_hours_none_and_garbage_fail_safe():
    # Missing/unparseable stamps return None so staleness can only relax, never
    # tighten, the gate (caller treats None as "fresh").
    assert _model_age_hours(None) is None
    assert _model_age_hours("") is None
    assert _model_age_hours("not-a-date") is None
