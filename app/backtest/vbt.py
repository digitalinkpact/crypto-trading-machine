"""Minimal vectorbt adapter.

Takes an OHLCV DataFrame plus boolean entry/exit series and returns a portfolio
metrics dict. Synchronous on purpose — vectorbt is CPU-bound numpy.
"""
from __future__ import annotations

import warnings
from typing import Any

import pandas as pd
import plotly.graph_objects as go


def _import_vectorbt_compat() -> Any:
    """Import vectorbt while tolerating plotly 6 template incompatibilities.

    vectorbt 0.26 registers bundled Plotly templates on import and still
    references the removed ``heatmapgl`` trace. In environments that drift to
    Plotly 6 despite our pin, allow Plotly to skip invalid template keys so the
    backtest adapter remains importable.
    """
    template_ctor = go.layout.Template

    class _CompatTemplate(template_ctor):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("skip_invalid", True)
            super().__init__(*args, **kwargs)

    go.layout.Template = _CompatTemplate  # type: ignore[assignment]
    try:
        import vectorbt as vectorbt_module  # type: ignore[import-untyped]
    finally:
        go.layout.Template = template_ctor  # type: ignore[assignment]
    return vectorbt_module


vbt = _import_vectorbt_compat()

from app.config import get_settings


# pandas' infer_freq returns anchored aliases (e.g. "W-SUN" for weekly, "M" /
# "MS" for monthly) that vectorbt cannot convert to a Timedelta — it raises a
# KeyError deep in pandas' parse_timedelta_unit. Map those to a fixed span so
# weekly/monthly backtests don't crash.
def _as_timedelta_freq(freq: str) -> str:
    try:
        with warnings.catch_warnings():
            # "H"/"M" lowercase deprecations warn but still convert fine; only
            # genuinely unconvertible (anchored) aliases should fall through.
            warnings.simplefilter("ignore")
            pd.Timedelta(freq)
        return freq
    except Exception as e:  # noqa: BLE001 — pandas raises ValueError/KeyError by version
        import logging
        logger = logging.getLogger(__name__)
        logger.warning("frequency conversion fallback for '%s': %s", freq, e)
        f = freq.upper()
        if f.startswith("W"):
            return "7D"
        if f.startswith(("M", "BM", "MS")):
            return "30D"
        if f.startswith("Q"):
            return "90D"
        if f.startswith(("A", "Y")):
            return "365D"
        return "1D"


def run_vectorbt_backtest(
    df: pd.DataFrame,
    entries: pd.Series,
    exits: pd.Series,
    init_cash: float = 10_000.0,
    fees: float | None = None,
    sl_stop: float | None = None,
    tp_stop: float | None = None,
    freq: str | None = None,
) -> dict[str, Any]:
    if fees is None:
        # Market entries/exits — charge taker on both sides.
        fees = get_settings().binance_taker_fee
    kwargs: dict[str, Any] = {
        "close": df["close"],
        "entries": entries,
        "exits": exits,
        "init_cash": init_cash,
        "fees": fees,
        "freq": _as_timedelta_freq(freq or pd.infer_freq(df.index) or "1H"),
    }
    if sl_stop is not None:
        kwargs["sl_stop"] = sl_stop
    if tp_stop is not None:
        kwargs["tp_stop"] = tp_stop
    pf = vbt.Portfolio.from_signals(**kwargs)
    stats = pf.stats()
    return {
        "total_return": float(stats.get("Total Return [%]", 0.0)) / 100.0,
        "sharpe": float(stats.get("Sharpe Ratio", 0.0)),
        "max_drawdown": float(stats.get("Max Drawdown [%]", 0.0)) / 100.0,
        "win_rate": float(stats.get("Win Rate [%]", 0.0)) / 100.0,
        "trades": int(stats.get("Total Trades", 0)),
    }
