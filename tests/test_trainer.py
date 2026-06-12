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
