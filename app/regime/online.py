"""Online regime model → dynamic confidence threshold.

The heuristic `RegimeClassifier` (EMA slope + ATR) stays the baseline label. On
top of it, this module learns from recently *resolved* trades and nudges the
global entry bar (`min_signal_confidence`):

  * favorable regime (model + realized win-rate high)  → lower the bar a touch
    (risk-on, take more of the edge),
  * unfavorable regime (losses piling up / high vol)   → raise the bar
    (risk-off, demand stronger signals).

It is deliberately bounded by `dynamic_threshold_max_delta` so it can never
override the technicals-based core — it only leans. With too little data it
returns a zero delta (neutral), so behavior is unchanged until the bot has a
track record.

Features (those persisted per signal event): volatility (atr_pct), trend
strength (ema_gap_pct), rsi_14, and the agent confidence. Spread and funding
are folded in at decision time by the caller when available.
"""
from __future__ import annotations

import time
from typing import Optional

import numpy as np

from app.config import Settings, get_settings
from app.logging_setup import get_logger
from app.storage import storage

log = get_logger(__name__)

_RECOMPUTE_SECONDS = 300.0  # retrain at most every 5 min — cheap but not per-tick
_WINDOW = 100               # learn from the last N resolved trades ("last 100 trades")


class OnlineRegime:
    """Lightweight online logistic model over recent resolved trades."""

    def __init__(self, settings: Optional[Settings] = None) -> None:
        self._settings = settings or get_settings()
        self._last_compute: float = 0.0
        self._cached_delta: float = 0.0
        self._cached_info: str = "init"

    def threshold_delta(self) -> tuple[float, str]:
        """Bounded additive delta for the min-confidence bar (cached ~5 min)."""
        s = self._settings
        if not s.dynamic_threshold_enabled:
            return 0.0, "disabled"
        now = time.time()
        if (now - self._last_compute) < _RECOMPUTE_SECONDS:
            return self._cached_delta, self._cached_info
        self._last_compute = now
        try:
            self._cached_delta, self._cached_info = self._compute()
        except Exception as exc:  # noqa: BLE001
            log.debug("[REGIME] online compute failed: %s", exc)
            self._cached_delta, self._cached_info = 0.0, f"error:{exc}"
        return self._cached_delta, self._cached_info

    def _compute(self) -> tuple[float, str]:
        s = self._settings
        rows = storage.training_signal_rows(limit=_WINDOW)
        if len(rows) < s.online_regime_min_samples:
            return 0.0, f"insufficient({len(rows)}/{s.online_regime_min_samples})"

        x = np.asarray([
            [
                float(r.get("atr_pct") or 0.0),       # volatility
                float(r.get("ema_gap_pct") or 0.0),   # trend strength
                float(r.get("rsi_14") or 50.0),
                float(r.get("confidence") or 0.0),
            ]
            for r in rows
        ], dtype=float)
        y = np.asarray([int(r.get("outcome_win") or 0) for r in rows], dtype=int)

        win_rate = float(y.mean())
        # Need both classes to fit a classifier; otherwise lean on win-rate only.
        p_hat = win_rate
        if len(np.unique(y)) == 2:
            from sklearn.linear_model import LogisticRegression
            from sklearn.pipeline import make_pipeline
            from sklearn.preprocessing import StandardScaler

            model = make_pipeline(
                StandardScaler(),
                LogisticRegression(max_iter=200, C=1.0),
            )
            model.fit(x, y)
            p_hat = float(model.predict_proba(x)[:, 1].mean())

        # Regime favorability blends the model's mean win-prob with the realized
        # win-rate, then penalizes elevated volatility (risk-off in chop spikes).
        favorability = 0.5 * p_hat + 0.5 * win_rate
        vol = float(np.median(x[:, 0]))  # median atr_pct over the window
        vol_penalty = min(0.10, max(0.0, (vol - 0.03) * 1.0))  # >3% ATR → lean risk-off
        favorability = max(0.0, min(1.0, favorability - vol_penalty))

        # Map favorability∈[0,1] → delta∈[-max,+max]. 0.5 = neutral.
        max_delta = s.dynamic_threshold_max_delta
        delta = max_delta * (1.0 - 2.0 * favorability)
        delta = max(-max_delta, min(max_delta, delta))
        info = (
            f"n={len(rows)} win_rate={win_rate:.2f} p_hat={p_hat:.2f} "
            f"vol={vol:.3f} favorability={favorability:.2f} delta={delta:+.3f}"
        )
        return delta, info


# Process-wide singleton.
online_regime = OnlineRegime()
