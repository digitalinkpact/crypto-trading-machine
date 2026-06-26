"""Signal-outcome labeling and lightweight supervised trainer.

This module is intentionally conservative: it labels matured events and trains
one small classifier used as an auxiliary confidence model.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, roc_auc_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.config import get_settings
from app.exchange import BinanceUSClient
from app.logging_setup import get_logger
from app.storage import storage
from app.trading.paper import paper_exchange

log = get_logger(__name__)

_MODEL_NAME = "signal_quality_v1"
_LAST_TRAINED_RESOLVED_KEY = "ml_last_trained_resolved_count"


def _event_return_pct(action: str, entry: float, current: float) -> float:
    if entry <= 0:
        return 0.0
    raw = (current - entry) / entry
    if action == "SELL":
        raw = -raw
    return float(raw)


def _min_win_edge(s) -> float:
    """Minimum return a matured signal must clear to count as a win.

    A market entry and its eventual exit each pay the taker fee, so the
    break-even bar is ``2 * taker_fee``; ``ml_label_slippage_pct`` adds a
    buffer for slippage. Returns below this are net losses and must be
    labeled as such, otherwise the quality gate is trained on a target that
    ignores the cost of trading.
    """
    return 2.0 * float(s.binance_taker_fee) + float(s.ml_label_slippage_pct)


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
        # Cost-aware label: a win must clear round-trip fees plus a slippage
        # buffer, not merely be positive. A bare > 0 dead-zone taught the gate
        # that fee-losing trades were wins.
        win = ret > _min_win_edge(s)
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

    # Chronological holdout — `training_signal_rows` returns oldest-first, so
    # the last 20% is the most recent data. A shuffled/stratified split would
    # leak future outcomes into the validation set and inflate the reported
    # AUC; the gate would then look better than it is on live, forward data.
    split_idx = max(1, int(len(x) * 0.8))
    x_train, x_test = x[:split_idx], x[split_idx:]
    y_train, y_test = y[:split_idx], y[split_idx:]

    # The time-ordered split can land all of one class on the train side. The
    # model still needs both classes to fit, so fall back to fitting on every
    # row for this round rather than shipping an un-fittable model. We lose the
    # honest holdout this time, flagged via `eval` below.
    if np.unique(y_train).size < 2:
        x_train, y_train = x, y

    model = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", LogisticRegression(max_iter=1000, class_weight="balanced")),
    ])
    model.fit(x_train, y_train)

    # AUC is only defined when the holdout has both classes. When it doesn't,
    # report a neutral 0.5 (no demonstrated skill) so the staleness/threshold
    # logic downstream never treats a degenerate round as a strong model.
    if x_test.shape[0] > 0 and np.unique(y_test).size >= 2:
        pred = model.predict(x_test)
        proba = model.predict_proba(x_test)[:, 1]
        accuracy = float(accuracy_score(y_test, pred))
        roc_auc = float(roc_auc_score(y_test, proba))
        eval_note = "chronological_holdout"
    else:
        accuracy = float(y.mean())
        roc_auc = 0.5
        eval_note = "degenerate_holdout"

    metrics = {
        "samples": int(len(rows)),
        "train_samples": int(x_train.shape[0]),
        "test_samples": int(x_test.shape[0]),
        "accuracy": accuracy,
        "roc_auc": roc_auc,
        "positive_rate": float(y.mean()),
        "eval": eval_note,
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
    total_resolved = int(after)
    latest_version = int(storage.latest_model_version(_MODEL_NAME))
    last_trained_resolved = int(
        storage.kv_get(_LAST_TRAINED_RESOLVED_KEY, default=0) or 0
    )
    since_last_train = max(0, total_resolved - last_trained_resolved)

    # Train first model as soon as we have enough data, even if each pass only
    # contributes a small number of new labels.
    if latest_version == 0:
        rows = storage.training_signal_rows(limit=100_000)
        if len(rows) < s.ml_min_training_samples:
            result["total_resolved"] = total_resolved
            result["since_last_train"] = since_last_train
            return result
    elif since_last_train < s.ml_min_new_labels:
        result["total_resolved"] = total_resolved
        result["since_last_train"] = since_last_train
        return result

    train_result = train_signal_quality_model()
    train_result["labeled"] = int(labeled)
    train_result["new_labels"] = int(new_labels)
    train_result["total_resolved"] = total_resolved
    train_result["since_last_train"] = since_last_train
    if train_result.get("status") == "ok":
        storage.kv_set(_LAST_TRAINED_RESOLVED_KEY, total_resolved)
    return train_result
