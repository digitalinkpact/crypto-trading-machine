"""Small statistics helpers for the read-only research layer.

Kept dependency-light (numpy only) and pure so they are trivially testable and
safe to import from analysis scripts. No I/O, no trading side effects.
"""
from __future__ import annotations

from typing import Sequence

import numpy as np


def bootstrap_ci(
    values: Sequence[float],
    *,
    statistic: str = "mean",
    n_resamples: int = 2000,
    alpha: float = 0.05,
    seed: int = 12345,
) -> tuple[float, float]:
    """Percentile bootstrap confidence interval for a sample statistic.

    Returns (low, high) at the ``1 - alpha`` confidence level. ``statistic`` is
    one of ``"mean"`` or ``"median"``. Resampling is seeded so reports are
    reproducible. With fewer than two data points the CI collapses to the point
    value (or NaN when empty) — callers should treat small samples as
    "insufficient evidence" separately.
    """
    arr = np.asarray([float(v) for v in values], dtype=float)
    if arr.size == 0:
        return float("nan"), float("nan")
    if arr.size == 1:
        return float(arr[0]), float(arr[0])

    rng = np.random.default_rng(seed)
    idx = rng.integers(0, arr.size, size=(n_resamples, arr.size))
    samples = arr[idx]
    if statistic == "median":
        stats = np.median(samples, axis=1)
    else:
        stats = samples.mean(axis=1)
    lo = float(np.percentile(stats, 100.0 * (alpha / 2.0)))
    hi = float(np.percentile(stats, 100.0 * (1.0 - alpha / 2.0)))
    return lo, hi


def expectancy(pnls: Sequence[float]) -> float:
    """Mean PnL per trade (expectancy). 0.0 for an empty sample."""
    arr = np.asarray([float(v) for v in pnls], dtype=float)
    return float(arr.mean()) if arr.size else 0.0


def profit_factor(pnls: Sequence[float]) -> float:
    """Gross profit / gross loss. ``inf`` when there are only winners, 0.0 when
    there are no winners at all."""
    arr = np.asarray([float(v) for v in pnls], dtype=float)
    gross_profit = float(arr[arr > 0].sum())
    gross_loss = float(-arr[arr < 0].sum())
    if gross_loss <= 0:
        return float("inf") if gross_profit > 0 else 0.0
    return gross_profit / gross_loss


def win_rate(pnls: Sequence[float]) -> float:
    arr = np.asarray([float(v) for v in pnls], dtype=float)
    return float((arr > 0).mean()) if arr.size else 0.0
