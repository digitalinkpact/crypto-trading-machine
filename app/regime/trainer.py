"""Signal-outcome labeling and lightweight supervised trainer.

This module is intentionally conservative: it labels matured events and trains
one small classifier used as an auxiliary confidence model.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.config import get_settings
from app.exchange import BinanceUSClient
from app.logging_setup import get_logger
from app.storage import storage
from app.trading.paper import paper_exchange

log = get_logger(__name__)

_MODEL_NAME = "signal_quality_v1"


def _event_return_pct(action: str, entry: float, current: float) -> float:
    if entry <= 0:
        return 0.0
    raw = (current - entry) / entry
    if action == "SELL":
        raw = -raw
    return float(raw)


async def label_matured_signal_events(limit: int = 500) -> int:
    """Resolve unresolved signal events older than the configured horizon."""
    s = get_settings()
    if not s.ml_learning_enabled:
        return 0

    horizon = timedelta(minutes=s.ml_signal_horizon_minutes)
    cutoff = (datetime.now(timezone.utc) - horizon).isoformat()
    pending = storage.pending_signal_events(older_than_iso=cutoff, limit=limit)
    if not pending:
        return 0

    live_client = BinanceUSClient()
    resolved = 0
    for ev in pending:
        symbol = ev["symbol"]
        mode = ev.get("mode") or "paper"
        try:
            if mode == "paper":
                px_now = float(await paper_exchange.ticker_price(symbol))
            else:
                px_now = float(await live_client.ticker_price(symbol))
        except Exception as exc:  # noqa: BLE001
            log.debug("label skip %s: price fetch failed: %s", symbol, exc)
            continue

        ret = _event_return_pct(ev["action"], float(ev["entry_price"]), px_now)
        # Small dead-zone avoids noisy labels around zero return.
        win = ret > 0.001
        storage.resolve_signal_event(
            event_id=int(ev["id"]),
            horizon_minutes=s.ml_signal_horizon_minutes,
            outcome_return_pct=ret,
            outcome_win=win,
        )
        resolved += 1

    if resolved:
        log.info("ml labeling: resolved %d matured signal events", resolved)
    return resolved


def _rows_to_xy(rows: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    x = []
    y = []
    for r in rows:
        action = 1.0 if r["action"] == "BUY" else 0.0
        tf = str(r.get("timeframe") or "1d")
        tf_weight = {
            "1h": 1.0,
            "4h": 1.5,
            "1d": 2.5,
            "1w": 4.0,
        }.get(tf, 1.0)
        x.append([
            float(r.get("confidence") or 0.0),
            float(r.get("atr_pct") or 0.0),
            float(r.get("rsi_14") or 50.0),
            float(r.get("ema_gap_pct") or 0.0),
            float(r.get("agent_count") or 0),
            tf_weight,
            action,
        ])
        y.append(int(r.get("outcome_win") or 0))
    return np.asarray(x, dtype=float), np.asarray(y, dtype=int)


def train_signal_quality_model() -> dict[str, float | int | str]:
    """Train and persist a small classifier for signal quality probability."""
    s = get_settings()
    if not s.ml_learning_enabled:
        return {"status": "disabled"}

    rows = storage.training_signal_rows(limit=100_000)
    if len(rows) < s.ml_min_training_samples:
        return {
            "status": "insufficient_data",
            "samples": len(rows),
            "min_required": s.ml_min_training_samples,
        }

    x, y = _rows_to_xy(rows)
    classes = np.unique(y)
    if classes.size < 2:
        return {
            "status": "single_class",
            "samples": int(y.size),
            "class": int(classes[0]) if classes.size == 1 else -1,
        }

    x_train, x_test, y_train, y_test = train_test_split(
        x, y, test_size=0.2, random_state=42, stratify=y
    )
    model = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", LogisticRegression(max_iter=1000, class_weight="balanced")),
    ])
    model.fit(x_train, y_train)

    pred = model.predict(x_test)
    proba = model.predict_proba(x_test)[:, 1]

    metrics = {
        "samples": int(len(rows)),
        "train_samples": int(x_train.shape[0]),
        "test_samples": int(x_test.shape[0]),
        "accuracy": float(accuracy_score(y_test, pred)),
        "roc_auc": float(roc_auc_score(y_test, proba)),
        "positive_rate": float(y.mean()),
    }
    version = storage.save_model_artifact(
        name=_MODEL_NAME,
        algorithm="logistic_regression",
        metrics=metrics,
        model=model,
    )
    metrics["version"] = int(version)
    metrics["status"] = "ok"
    log.info(
        "ml train: model=%s version=%s samples=%s auc=%.3f",
        _MODEL_NAME,
        version,
        metrics["samples"],
        metrics["roc_auc"],
    )
    return metrics


async def run_learning_cycle() -> dict[str, float | int | str]:
    """Label matured events and retrain when enough new labels exist."""
    s = get_settings()
    if not s.ml_learning_enabled:
        return {"status": "disabled"}

    before = storage.count_resolved_signal_events()
    labeled = await label_matured_signal_events(limit=1000)
    after = storage.count_resolved_signal_events()
    new_labels = max(0, after - before)

    result: dict[str, float | int | str] = {
        "status": "labeled_only",
        "labeled": int(labeled),
        "new_labels": int(new_labels),
    }
    if new_labels < s.ml_min_new_labels:
        return result

    train_result = train_signal_quality_model()
    train_result["labeled"] = int(labeled)
    train_result["new_labels"] = int(new_labels)
    return train_result
