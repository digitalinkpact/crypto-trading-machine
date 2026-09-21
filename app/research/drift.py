"""Stage 3 — Live vs backtest drift monitor (READ-ONLY, pure logic).

Compares live trading behaviour against the walk-forward backtest expectation
and flags *material* drift, separating:

* **strategy drift** — the strategy's own results (expectancy, win rate, exit-
  reason mix, drawdown) diverging from what the backtest predicted, and
* **execution drift** — real fills costing more than the backtest modeled
  (spread at entry / slippage vs the modeled slippage assumption).

Everything here is pure and read-only: no I/O, no order path, no config change.
The CLI (``scripts/drift_report.py``) supplies live data and a baseline.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Reuse the same modeled-slippage assumption the Stage 1 simulator uses so the
# "backtest expected cost" is consistent across the research layer.
from app.research.rejected_setups import DEFAULT_SLIPPAGE_PCT


@dataclass
class DriftThresholds:
    min_trades: int = 30
    win_rate_delta: float = 0.10       # 10 percentage points
    drawdown_factor: float = 1.5       # live dd > 1.5x backtest dd
    drawdown_min_abs: float = 0.05     # and at least +5pp worse
    exit_mix_delta: float = 0.15       # 15 percentage points share
    slippage_delta: float = 0.0015     # live entry spread > modeled + 15 bps


@dataclass
class BacktestBaseline:
    expectancy_pct: float
    win_rate: float
    max_drawdown_pct: float
    modeled_slippage_pct: float = DEFAULT_SLIPPAGE_PCT
    exit_reason_mix: dict[str, float] = field(default_factory=dict)
    n_trades: Optional[int] = None
    source: str = "unspecified"


def default_baseline() -> BacktestBaseline:
    """A documented walk-forward expectation for the live daily dip-buy strategy.

    These are REFERENCE values from the repository's own strategy_lab / walk-
    forward findings (2026-09-16 R:R sweep: ~+7.4%/trade expectancy, ~65% win,
    realized max drawdown well under 15%). They are deliberately conservative
    placeholders — for a rigorous comparison pass a fresh baseline with
    ``--backtest-json`` produced from the current walk-forward run. Exit-reason
    mix is left empty so exit-mix drift is only evaluated when a real baseline
    supplies it.
    """
    return BacktestBaseline(
        expectancy_pct=0.0744,
        win_rate=0.65,
        max_drawdown_pct=0.013,
        modeled_slippage_pct=DEFAULT_SLIPPAGE_PCT,
        exit_reason_mix={},
        source="documented strategy_lab daily dip-buy expectation (override with --backtest-json)",
    )


def load_baseline_from_json(path: Path) -> BacktestBaseline:
    data = json.loads(Path(path).read_text())
    return BacktestBaseline(
        expectancy_pct=float(data["expectancy_pct"]),
        win_rate=float(data["win_rate"]),
        max_drawdown_pct=float(data["max_drawdown_pct"]),
        modeled_slippage_pct=float(data.get("modeled_slippage_pct", DEFAULT_SLIPPAGE_PCT)),
        exit_reason_mix={str(k): float(v) for k, v in (data.get("exit_reason_mix") or {}).items()},
        n_trades=data.get("n_trades"),
        source=str(data.get("source", str(path))),
    )


@dataclass
class LiveSnapshot:
    n_trades: int
    expectancy_pct: float
    expectancy_ci: tuple[float, float]
    win_rate: float
    max_drawdown_pct: float
    exit_reason_mix: dict[str, float]
    avg_entry_spread_pct: Optional[float]
    modeled_fee_pct: float
    window_label: str = "all history"


# ── pure helpers ────────────────────────────────────────────────────────────

def pooled_max_drawdown_pct(pnl_fracs: list[float], *, risk_per_trade_pct: float = 0.01) -> float:
    """Peak-to-trough drawdown of a pooled equity curve where each trade risks a
    fixed fraction of equity — matching ``scripts/strategy_lab.aggregate`` so the
    live number is directly comparable to the backtest baseline. Fixed-fraction
    compounding also keeps a single outlier per-trade return (e.g. a +94% dust
    exit) from producing a nonsensical >100% drawdown. 0.0 for an empty list.
    """
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in pnl_fracs:
        equity *= (1.0 + r * risk_per_trade_pct)
        peak = max(peak, equity)
        if peak > 0:
            max_dd = max(max_dd, (peak - equity) / peak)
    return max_dd


def exit_mix(exit_reasons: list[str]) -> dict[str, float]:
    """Fractional share of each exit reason. Empty/None -> "unknown"."""
    if not exit_reasons:
        return {}
    counts: dict[str, int] = {}
    for r in exit_reasons:
        key = str(r or "") or "unknown"
        counts[key] = counts.get(key, 0) + 1
    total = len(exit_reasons)
    return {k: v / total for k, v in counts.items()}


def avg_entry_spread_pct(tick_rows: list[dict], *, mode: str = "live") -> Optional[float]:
    """Mean ``spread_pct`` at entry, from executed-BUY tick_audit rows.

    This is the taker's immediate half-spread cost at entry — the execution-cost
    proxy the backtest under-models when it assumes a flat slippage. Returns
    None when no executed BUY row carries a spread.
    """
    spreads: list[float] = []
    for row in tick_rows:
        if str(row.get("mode") or "") != mode:
            continue
        if str(row.get("action") or "") != "BUY" or int(row.get("executed") or 0) != 1:
            continue
        try:
            ind = json.loads(row.get("indicators") or "{}")
        except (TypeError, ValueError):
            continue
        sp = ind.get("spread_pct") if isinstance(ind, dict) else None
        if isinstance(sp, (int, float)):
            spreads.append(float(sp))
    return (sum(spreads) / len(spreads)) if spreads else None


# ── drift computation ───────────────────────────────────────────────────────

@dataclass
class DriftMetric:
    name: str
    kind: str  # "strategy" | "execution"
    live: Optional[float]
    backtest: Optional[float]
    delta: Optional[float]
    material: bool
    detail: str


@dataclass
class DriftReport:
    metrics: list[DriftMetric]
    strategy_drift: bool
    execution_drift: bool
    n_live_trades: int
    notes: list[str]


def compute_drift(
    live: LiveSnapshot,
    baseline: BacktestBaseline,
    thresholds: Optional[DriftThresholds] = None,
) -> DriftReport:
    t = thresholds or DriftThresholds()
    metrics: list[DriftMetric] = []
    notes: list[str] = []
    adequate = live.n_trades >= t.min_trades
    if not adequate:
        notes.append(
            f"live sample is small (n={live.n_trades} < {t.min_trades}); strategy-drift "
            "flags are advisory and may be noise."
        )

    # Expectancy — material only when the backtest value lies OUTSIDE the live
    # bootstrap CI (statistically distinguishable) and the sample is adequate.
    ci_lo, ci_hi = live.expectancy_ci
    exp_delta = live.expectancy_pct - baseline.expectancy_pct
    exp_material = adequate and not (ci_lo <= baseline.expectancy_pct <= ci_hi)
    metrics.append(DriftMetric(
        "expectancy_pct", "strategy", live.expectancy_pct, baseline.expectancy_pct, exp_delta,
        exp_material,
        f"live {live.expectancy_pct:+.2%} (95% CI [{ci_lo:+.2%},{ci_hi:+.2%}], n={live.n_trades}) "
        f"vs backtest {baseline.expectancy_pct:+.2%}",
    ))

    # Win rate.
    wr_delta = live.win_rate - baseline.win_rate
    wr_material = adequate and abs(wr_delta) > t.win_rate_delta
    metrics.append(DriftMetric(
        "win_rate", "strategy", live.win_rate, baseline.win_rate, wr_delta, wr_material,
        f"live {live.win_rate:.1%} vs backtest {baseline.win_rate:.1%}",
    ))

    # Drawdown — worse live drawdown flags strategy risk drift.
    dd_delta = live.max_drawdown_pct - baseline.max_drawdown_pct
    dd_material = (
        live.max_drawdown_pct > baseline.max_drawdown_pct * t.drawdown_factor
        and dd_delta > t.drawdown_min_abs
    )
    metrics.append(DriftMetric(
        "max_drawdown_pct", "strategy", live.max_drawdown_pct, baseline.max_drawdown_pct,
        dd_delta, dd_material,
        f"live {live.max_drawdown_pct:.1%} vs backtest {baseline.max_drawdown_pct:.1%} "
        "(pooled fixed-risk equity, matches strategy_lab)",
    ))

    # Exit-reason mix — only when the baseline supplies one.
    if baseline.exit_reason_mix:
        for reason in sorted(set(baseline.exit_reason_mix) | set(live.exit_reason_mix)):
            lv = live.exit_reason_mix.get(reason, 0.0)
            bt = baseline.exit_reason_mix.get(reason, 0.0)
            d = lv - bt
            material = adequate and abs(d) > t.exit_mix_delta
            metrics.append(DriftMetric(
                f"exit_mix:{reason}", "strategy", lv, bt, d, material,
                f"live {lv:.0%} vs backtest {bt:.0%}",
            ))

    # Execution — entry spread vs modeled slippage.
    if live.avg_entry_spread_pct is not None:
        slip_delta = live.avg_entry_spread_pct - baseline.modeled_slippage_pct
        slip_material = slip_delta > t.slippage_delta
        metrics.append(DriftMetric(
            "entry_execution_cost", "execution", live.avg_entry_spread_pct,
            baseline.modeled_slippage_pct, slip_delta, slip_material,
            f"live avg entry spread {live.avg_entry_spread_pct:.3%} vs modeled slippage "
            f"{baseline.modeled_slippage_pct:.3%}",
        ))
        # Interpretive note: does execution cost explain the expectancy gap?
        exp_gap = baseline.expectancy_pct - live.expectancy_pct
        exec_cost_gap = 2.0 * max(0.0, slip_delta)  # round-trip extra cost
        if exp_gap > 0 and exec_cost_gap > 0:
            share = min(1.0, exec_cost_gap / exp_gap)
            notes.append(
                f"under-modeled execution cost (~{exec_cost_gap:.2%} round-trip) could account "
                f"for ~{share:.0%} of the {exp_gap:.2%} live-vs-backtest expectancy gap; "
                "the remainder is strategy drift."
            )
    else:
        notes.append("no executed-BUY spread data available — execution drift not assessed.")

    strategy_drift = any(m.material for m in metrics if m.kind == "strategy")
    execution_drift = any(m.material for m in metrics if m.kind == "execution")
    return DriftReport(
        metrics=metrics,
        strategy_drift=strategy_drift,
        execution_drift=execution_drift,
        n_live_trades=live.n_trades,
        notes=notes,
    )


# ── live snapshot builder (read-only orchestration) ─────────────────────────

def filter_window(trades: list[dict], window_days: int) -> tuple[list[dict], str]:
    """Keep trades whose ``exit_ts`` is within the last ``window_days`` days
    (``0`` = all history). Returns (trades, human label)."""
    if window_days <= 0:
        return trades, "all history"
    from datetime import datetime, timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    kept = []
    for t in trades:
        try:
            ts = datetime.fromisoformat(str(t.get("exit_ts")))
        except (TypeError, ValueError):
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts >= cutoff:
            kept.append(t)
    return kept, f"last {window_days} days"


def build_live_snapshot(
    *,
    mode: str = "live",
    window_days: int = 30,
    source_storage=None,
    tick_rows: Optional[list[dict]] = None,
) -> LiveSnapshot:
    """Assemble a ``LiveSnapshot`` from live ``closed_trades`` + ``tick_audit``.

    Read-only orchestration used by both ``scripts/drift_report.py`` and
    ``scripts/weekly_experiment_report.py``. Storage/settings are imported
    lazily so the pure comparison core above stays import-light and testable.
    """
    from app.config import get_settings
    from app.storage import storage as _live_storage
    from app.trading.experiment_stats import compute_stats, load_live_trades, trade_pnl_pct

    src = source_storage or _live_storage
    all_trades = load_live_trades(mode=mode, source_storage=src)
    trades, label = filter_window(all_trades, window_days)
    stats = compute_stats(trades)
    rows = tick_rows if tick_rows is not None else src.recent_tick_audit(limit=200_000)
    return LiveSnapshot(
        n_trades=stats.n,
        expectancy_pct=stats.expectancy_pct,
        expectancy_ci=stats.expectancy_pct_ci,
        win_rate=stats.win_rate,
        max_drawdown_pct=pooled_max_drawdown_pct([trade_pnl_pct(t) for t in trades]),
        exit_reason_mix=exit_mix([str(t.get("exit_reason") or "") for t in trades]),
        avg_entry_spread_pct=avg_entry_spread_pct(rows, mode=mode),
        modeled_fee_pct=float(get_settings().binance_taker_fee),
        window_label=label,
    )
