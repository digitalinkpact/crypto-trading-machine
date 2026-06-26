from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.regime import trainer


@pytest.fixture
def learning_enabled(monkeypatch):
    monkeypatch.setattr(
        trainer,
        "get_settings",
        lambda: SimpleNamespace(
            ml_learning_enabled=True,
            ml_min_new_labels=50,
            ml_min_training_samples=200,
        ),
    )


async def test_run_learning_cycle_trains_when_cumulative_labels_cross_threshold(
    monkeypatch, learning_enabled
):
    async def _fake_label_matured_signal_events(limit=1000):
        return 10

    monkeypatch.setattr(trainer, "label_matured_signal_events", _fake_label_matured_signal_events)

    counts = iter([120, 130])
    monkeypatch.setattr(trainer.storage, "count_resolved_signal_events", lambda: next(counts))
    monkeypatch.setattr(trainer.storage, "latest_model_version", lambda _name: 3)
    monkeypatch.setattr(
        trainer.storage,
        "kv_get",
        lambda key, default=0: 70 if key == "ml_last_trained_resolved_count" else default,
    )

    kv_writes: list[tuple[str, int]] = []
    monkeypatch.setattr(trainer.storage, "kv_set", lambda key, value: kv_writes.append((key, value)))

    monkeypatch.setattr(
        trainer,
        "train_signal_quality_model",
        lambda: {"status": "ok", "version": 4, "samples": 500},
    )

    result = await trainer.run_learning_cycle()

    assert result["status"] == "ok"
    assert result["since_last_train"] == 60
    assert ("ml_last_trained_resolved_count", 130) in kv_writes


async def test_run_learning_cycle_skips_when_cumulative_labels_below_threshold(
    monkeypatch, learning_enabled
):
    async def _fake_label_matured_signal_events(limit=1000):
        return 5

    monkeypatch.setattr(trainer, "label_matured_signal_events", _fake_label_matured_signal_events)

    counts = iter([100, 105])
    monkeypatch.setattr(trainer.storage, "count_resolved_signal_events", lambda: next(counts))
    monkeypatch.setattr(trainer.storage, "latest_model_version", lambda _name: 2)
    monkeypatch.setattr(
        trainer.storage,
        "kv_get",
        lambda key, default=0: 80 if key == "ml_last_trained_resolved_count" else default,
    )

    called = {"train": False}

    def _fake_train():
        called["train"] = True
        return {"status": "ok"}

    monkeypatch.setattr(trainer, "train_signal_quality_model", _fake_train)

    result = await trainer.run_learning_cycle()

    assert result["status"] == "labeled_only"
    assert result["since_last_train"] == 25
    assert called["train"] is False


def test_min_win_edge_clears_round_trip_fees_plus_slippage():
    s = SimpleNamespace(binance_taker_fee=0.0040, ml_label_slippage_pct=0.0010)
    # 2 * 0.0040 + 0.0010 = 0.0090
    assert trainer._min_win_edge(s) == pytest.approx(0.0090)


def test_min_win_edge_marks_fee_losing_trade_as_loss():
    s = SimpleNamespace(binance_taker_fee=0.0040, ml_label_slippage_pct=0.0010)
    edge = trainer._min_win_edge(s)
    # A +0.5% move does not clear ~0.9% round-trip cost → not a win.
    assert (0.005 > edge) is False
    # A +1.2% move clears it → a win.
    assert (0.012 > edge) is True


def test_train_uses_chronological_holdout(monkeypatch):
    monkeypatch.setattr(
        trainer,
        "get_settings",
        lambda: SimpleNamespace(
            ml_learning_enabled=True,
            ml_min_training_samples=200,
        ),
    )

    # Build 250 separable, time-ordered rows. High confidence + positive
    # ema_gap → win; low confidence + negative ema_gap → loss. Alternating so
    # both classes appear across the whole timeline (and thus in the last-20%
    # chronological holdout).
    rows = []
    for i in range(250):
        win = i % 2 == 0
        rows.append({
            "action": "BUY",
            "timeframe": "1d",
            "confidence": 0.9 if win else 0.2,
            "atr_pct": 0.02,
            "rsi_14": 60.0 if win else 40.0,
            "ema_gap_pct": 0.01 if win else -0.01,
            "agent_count": 3,
            "outcome_win": 1 if win else 0,
        })

    monkeypatch.setattr(trainer.storage, "training_signal_rows", lambda limit=100_000: rows)
    saved: dict = {}

    def _fake_save(*, name, algorithm, metrics, model):
        saved["metrics"] = metrics
        return 7

    monkeypatch.setattr(trainer.storage, "save_model_artifact", _fake_save)

    result = trainer.train_signal_quality_model()

    assert result["status"] == "ok"
    # 80/20 chronological split, no shuffle.
    assert result["train_samples"] == 200
    assert result["test_samples"] == 50
    assert result["eval"] == "chronological_holdout"
