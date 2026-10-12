"""Strategy lab — a single faithful event-driven simulator (entry -> stop_loss
/ TP1 / TP2 / trailing / mean-reversion-RSI-exit / stale_dead_money / max_hold,
exactly mirroring app/trading/risk.py's live exit ladder and
app/trading/strategy.py's signal exit) used to run every sweep this
optimization pass needs with an IDENTICAL execution model, so only the one
parameter under test actually varies:

    python -m scripts.strategy_lab entry            # dip_buy vs oversold_bounce
    python -m scripts.strategy_lab rsi_exit          # old vs new RSI-exit params
    python -m scripts.strategy_lab trailing          # activation x distance grid
    python -m scripts.strategy_lab stop_stale        # ATR-stop cap x stale-exit (per-position)
    python -m scripts.strategy_lab all               # everything, in the requested order

The stop is the LIVE ATR stop (clamp(atr_stop_multiple*atr_pct, min, max)); the
stale_dead_money exit (held >= stale_exit_days below stale_exit_max_pnl) is
modelled too, so the sim's exits match live. Metrics are reported at BOTH leg
level (= closed_trades rows) and POSITION level (partial-exit legs collapsed by
symbol+entry — the honest win rate).

Why a custom simulator instead of vectorbt (scripts/walkforward.py): vectorbt's
`Portfolio.from_signals` takes one fixed sl_stop/tp_stop and has no notion of
running per-position PnL for a signal-based exit — it cannot express the TP1
(partial)/TP2(partial)/trailing-with-activation ladder or the mean-reversion
exit's PnL-gate at all. This module walks each symbol bar-by-bar (daily closed
candles only, no look-ahead — every decision at bar i only reads df.iloc[:i+1])
applying the exact same rule order as the live code.

Read-only: fetches public klines via the existing OHLCVRepository cache, never
places orders or touches trading.db.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field, replace
from typing import Callable, Optional

import numpy as np
import pandas as pd

from app.config import Timeframe, get_settings
from scripts.walkforward import SYMBOLS, _fold_bounds, _load  # noqa: F401 (reuse loader/fold splitter)

EntryFn = Callable[[pd.DataFrame, int], bool]


# ── Entry conditions (mirror app/trading/strategy.py exactly) ──────────────

def entry_dip_buy(df: pd.DataFrame, i: int) -> bool:
    row = df.iloc[i]
    return bool(row["rsi_14"] < 30 and row["close"] <= row["bb_lower"])


def make_entry_oversold_bounce(rsi_max: float, bb_mult: float, min_bounce_pct: float) -> EntryFn:
    def fn(df: pd.DataFrame, i: int) -> bool:
        if i < 5:
            return False
        row = df.iloc[i]
        low5 = float(df["low"].iloc[max(0, i - 4): i + 1].min())
        if low5 <= 0:
            return False
        bounced = row["close"] > low5 * (1 + min_bounce_pct)
        return bool(
            row["rsi_14"] < rsi_max
            and row["close"] < row["bb_lower"] * bb_mult
            and bounced
        )
    return fn


entry_oversold_bounce = make_entry_oversold_bounce(40.0, 1.02, 0.05)


# ── Trade / params containers ───────────────────────────────────────────

@dataclass
class LadderParams:
    stop_loss_pct: float
    tp1_pct: float
    tp1_frac: float
    tp2_pct: float
    tp2_frac: float
    trail_activation: float
    trail_distance: float
    max_hold_days: int
    mean_reversion_rsi: float
    mean_reversion_min_pnl: float
    mean_reversion_require_confirmation: bool
    fee_rate: float
    # ATR-scaled stop (mirrors risk.dynamic_stop_pct): stop = clamp(mult*atr_pct,
    # min, max), computed from the entry bar's ATR. When disabled, the fixed
    # stop_loss_pct is used. This is the LIVE stop, so modelling it is required
    # to evaluate tightening atr_stop_max.
    atr_stop_enabled: bool = True
    atr_stop_min: float = 0.03
    atr_stop_max: float = 0.08
    atr_stop_multiple: float = 2.0
    # stale_dead_money exit (mirrors risk.evaluate_exits): force-close a position
    # held >= stale_exit_days whose gain is still below stale_exit_max_pnl.
    stale_exit_enabled: bool = True
    stale_exit_days: int = 2
    stale_exit_max_pnl: float = 0.02


def default_params(**overrides) -> LadderParams:
    s = get_settings()
    base = dict(
        stop_loss_pct=s.stop_loss_pct,
        tp1_pct=s.take_profit_1_pct,
        tp1_frac=s.take_profit_1_fraction,
        tp2_pct=s.take_profit_2_pct,
        tp2_frac=s.take_profit_2_fraction,
        trail_activation=s.trailing_activation_pct,
        trail_distance=s.trailing_stop_pct,
        max_hold_days=int(s.max_hold_hours / 24),
        mean_reversion_rsi=s.mean_reversion_exit_rsi,
        mean_reversion_min_pnl=s.mean_reversion_exit_min_pnl_pct,
        mean_reversion_require_confirmation=(
            s.mean_reversion_exit_require_momentum_confirmation
            or s.mean_reversion_exit_require_price_confirmation
        ),
        fee_rate=s.binance_taker_fee,
        atr_stop_enabled=getattr(s, "atr_stop_enabled", True),
        atr_stop_min=getattr(s, "atr_stop_min_pct", 0.03),
        atr_stop_max=getattr(s, "atr_stop_max_pct", 0.08),
        atr_stop_multiple=getattr(s, "atr_stop_multiple", 2.0),
        stale_exit_enabled=getattr(s, "stale_exit_enabled", True),
        stale_exit_days=max(1, int(getattr(s, "stale_exit_hours", 48) / 24)),
        stale_exit_max_pnl=getattr(s, "stale_exit_max_pnl_pct", 0.02),
    )
    base.update(overrides)
    return LadderParams(**base)


@dataclass
class Trade:
    symbol: str
    entry_idx: int
    exit_idx: int
    entry_price: float
    exit_price: float
    qty: float
    pnl_pct: float  # relative to this leg's own entry notional
    exit_reason: str
    mfe_pct: float
    mae_pct: float
    regime_risk_on: Optional[bool] = None


def _momentum_deteriorating(df: pd.DataFrame, i: int) -> bool:
    if i < 1 or "macd_hist" not in df.columns:
        return False
    return bool(df["macd_hist"].iloc[i] < df["macd_hist"].iloc[i - 1])


def _bearish_price_confirmation(df: pd.DataFrame, i: int) -> bool:
    if "ema_20" not in df.columns:
        return False
    row = df.iloc[i]
    return bool(row["close"] < row["ema_20"])


def _entry_stop_frac(df: pd.DataFrame, i: int, params: LadderParams) -> float:
    """ATR-scaled stop fraction at the entry bar (mirrors risk.dynamic_stop_pct).

    clamp(atr_stop_multiple * atr_pct, atr_stop_min, atr_stop_max). Falls back to
    the fixed stop_loss_pct when ATR is unavailable or the ATR stop is disabled.
    """
    if not params.atr_stop_enabled or "atr_14" not in df.columns:
        return params.stop_loss_pct
    close = float(df["close"].iloc[i])
    atr = float(df["atr_14"].iloc[i]) if not pd.isna(df["atr_14"].iloc[i]) else 0.0
    atr_pct = (atr / close) if close > 0 else 0.0
    if atr_pct <= 0:
        return params.stop_loss_pct
    raw = params.atr_stop_multiple * atr_pct
    return float(min(max(raw, params.atr_stop_min), params.atr_stop_max))


def simulate_symbol(
    symbol: str,
    df: pd.DataFrame,
    entry_fn: EntryFn,
    params: LadderParams,
    *,
    risk_on: Optional[pd.Series] = None,
    mean_reversion_priority: bool = False,
) -> list[Trade]:
    """Walk one symbol's daily candles bar-by-bar, applying the full live
    exit ladder (stop_loss -> TP1 -> TP2 -> trailing -> mean_reversion_rsi ->
    max_hold, same priority risk.py/strategy.py use) plus the BTC-regime entry
    gate. No look-ahead: entry_fn(df, i) and every exit check at bar i only
    reads df.iloc[:i+1].

    `mean_reversion_priority=True` checks the mean-reversion RSI exit BEFORE
    the price-based ladder instead of after. In production these are two
    independent, uncoordinated processes (the strategy tick vs. the 15s risk
    loop) racing on real intraday price, not one deterministic daily-bar
    order — this flag approximates "what if the RSI-exit signal won that
    race" as a bracketing scenario, since a single daily-close simulation
    can't reproduce the real race condition at all.
    """
    trades: list[Trade] = []
    entry_price: Optional[float] = None
    entry_idx: Optional[int] = None
    qty = 0.0
    original_qty = 0.0
    hwm = 0.0
    lwm = 0.0
    stop_frac = params.stop_loss_pct
    tp1_taken = False
    tp2_taken = False

    n = len(df)
    for i in range(n):
        row = df.iloc[i]
        price = float(row["close"])

        if entry_price is None:
            if risk_on is not None and not bool(risk_on.iloc[i]):
                continue
            if entry_fn(df, i):
                entry_price = price
                entry_idx = i
                qty = 1.0
                original_qty = 1.0
                hwm = price
                lwm = price
                stop_frac = _entry_stop_frac(df, i, params)
                tp1_taken = False
                tp2_taken = False
            continue

        hwm = max(hwm, price)
        lwm = min(lwm, price)
        change = (price - entry_price) / entry_price
        exit_reason: Optional[str] = None
        exit_qty = 0.0

        def _mean_reversion_hit() -> bool:
            return bool(
                row.get("rsi_14", 0) > params.mean_reversion_rsi
                and change >= params.mean_reversion_min_pnl
                and change < params.trail_activation  # defer to the ladder above this
                and (
                    not params.mean_reversion_require_confirmation
                    or _momentum_deteriorating(df, i)
                    or _bearish_price_confirmation(df, i)
                )
            )

        if mean_reversion_priority and _mean_reversion_hit():
            exit_reason, exit_qty = "mean_reversion_rsi", qty
        elif change <= -stop_frac:
            exit_reason, exit_qty = "stop_loss", qty
        elif not tp1_taken and change >= params.tp1_pct:
            exit_reason, exit_qty = "take_profit_1", original_qty * params.tp1_frac
            tp1_taken = True
        elif tp1_taken and not tp2_taken and change >= params.tp2_pct:
            exit_reason, exit_qty = "take_profit_2", min(qty, original_qty * params.tp2_frac)
            tp2_taken = True
        elif hwm > entry_price * (1 + params.trail_activation) and price <= hwm * (1 - params.trail_distance):
            exit_reason, exit_qty = "trailing_stop", qty
        elif (not mean_reversion_priority) and _mean_reversion_hit():
            exit_reason, exit_qty = "mean_reversion_rsi", qty
        elif (
            params.stale_exit_enabled
            and (i - entry_idx) >= params.stale_exit_days
            and change < params.stale_exit_max_pnl
        ):
            exit_reason, exit_qty = "stale_dead_money", qty
        elif (i - entry_idx) >= params.max_hold_days:
            exit_reason, exit_qty = "max_hold", qty

        if exit_reason:
            exit_qty = min(exit_qty, qty)
            if exit_qty <= 0:
                continue
            gross = exit_qty * (price - entry_price)
            fees = exit_qty * entry_price * params.fee_rate + exit_qty * price * params.fee_rate
            pnl = gross - fees
            entry_notional = exit_qty * entry_price
            pnl_pct = (pnl / entry_notional) if entry_notional else 0.0
            mfe_pct = (hwm - entry_price) / entry_price
            mae_pct = (entry_price - lwm) / entry_price
            trades.append(Trade(
                symbol=symbol, entry_idx=entry_idx, exit_idx=i,
                entry_price=entry_price, exit_price=price, qty=exit_qty,
                pnl_pct=pnl_pct, exit_reason=exit_reason, mfe_pct=mfe_pct, mae_pct=mae_pct,
            ))
            qty -= exit_qty
            if qty <= 1e-9 or exit_reason in ("stop_loss", "trailing_stop", "max_hold", "mean_reversion_rsi", "stale_dead_money"):
                entry_price = None
                entry_idx = None
                qty = 0.0

    return trades


# ── Aggregation ─────────────────────────────────────────────────────────

@dataclass
class Metrics:
    n: int = 0
    win_rate: float = 0.0
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    profit_factor: float = 0.0
    expectancy_pct: float = 0.0
    total_pnl_pct: float = 0.0
    max_drawdown_pct: float = 0.0
    max_losing_streak: int = 0
    mfe_captured_pct: float = 0.0  # avg(realized pnl% / mfe%) for winners, 0-1
    net_pnl: float = 0.0
    return_pct: float = 0.0
    time_in_market_pct: float = 0.0
    return_per_drawdown: float = 0.0


def aggregate(trades: list[Trade], *, risk_per_trade_pct: float = 0.01) -> Metrics:
    if not trades:
        return Metrics()
    ordered = sorted(trades, key=lambda t: t.exit_idx)
    pcts = [t.pnl_pct for t in ordered]
    wins = [p for p in pcts if p > 0]
    losses = [p for p in pcts if p <= 0]
    n = len(pcts)
    win_rate = len(wins) / n
    avg_win = (sum(wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf") if wins else 0.0
    expectancy = sum(pcts) / n

    # Pooled equity curve: each trade risks a fixed `risk_per_trade_pct` of
    # equity (matches the live fixed-risk sizing model) — NOT full notional.
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    streak = 0
    max_streak = 0
    for t in ordered:
        equity *= (1 + t.pnl_pct * risk_per_trade_pct)
        peak = max(peak, equity)
        dd = (peak - equity) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        if t.pnl_pct <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    mfe_ratios = [
        (max(t.pnl_pct, 0) / t.mfe_pct) for t in ordered
        if t.mfe_pct > 0 and t.exit_reason not in ("stop_loss",)
    ]
    mfe_captured = float(np.mean(mfe_ratios)) if mfe_ratios else 0.0

    net_return = equity - 1.0
    return Metrics(
        n=n, win_rate=win_rate, avg_win_pct=avg_win, avg_loss_pct=avg_loss,
        profit_factor=profit_factor, expectancy_pct=expectancy,
        total_pnl_pct=sum(pcts), max_drawdown_pct=max_dd,
        max_losing_streak=max_streak, mfe_captured_pct=mfe_captured,
        net_pnl=10000.0 * net_return,
        return_pct=net_return,
        return_per_drawdown=(net_return / max_dd) if max_dd > 0 else 0.0,
    )


def positions_from_trades(trades: list[Trade]) -> list[Trade]:
    """Collapse partial-exit legs (sharing symbol+entry_idx) into one synthetic
    POSITION-level Trade so win-rate/expectancy are per position, not per leg.

    A winner that scaled out across TP1/TP2/trailing legs is one position, not
    three wins. Position pnl% on the FULL original notional = Σ(leg pnl% * leg
    qty) since every leg shares the position's entry price.
    """
    groups: dict[tuple[str, int], list[Trade]] = defaultdict(list)
    for t in trades:
        groups[(t.symbol, t.entry_idx)].append(t)
    out: list[Trade] = []
    for (sym, eidx), legs in groups.items():
        pos_pnl_pct = sum(leg.pnl_pct * leg.qty for leg in legs)
        last = max(legs, key=lambda leg: leg.exit_idx)
        out.append(Trade(
            symbol=sym, entry_idx=eidx, exit_idx=last.exit_idx,
            entry_price=legs[0].entry_price, exit_price=last.exit_price,
            qty=1.0, pnl_pct=pos_pnl_pct, exit_reason=last.exit_reason,
            mfe_pct=max(leg.mfe_pct for leg in legs),
            mae_pct=max(leg.mae_pct for leg in legs),
            regime_risk_on=legs[0].regime_risk_on,
        ))
    return out


def aggregate_positions(trades: list[Trade]) -> Metrics:
    """Per-POSITION metrics (collapses partial-exit legs first)."""
    return aggregate(positions_from_trades(trades))


def exit_reason_mix(trades: list[Trade]) -> dict[str, int]:
    """Count POSITIONS by their final exit reason (how the slot was freed)."""
    mix: dict[str, int] = defaultdict(int)
    for p in positions_from_trades(trades):
        mix[p.exit_reason] += 1
    return dict(sorted(mix.items(), key=lambda kv: -kv[1]))


def print_metrics(label: str, m: Metrics) -> None:
    pf = f"{m.profit_factor:.2f}" if m.profit_factor != float("inf") else "inf"
    print(
        f"{label:<28} n={m.n:<5} win%={m.win_rate:>6.1%} pf={pf:>5} "
        f"expectancy={m.expectancy_pct:>+7.2%} avg_win={m.avg_win_pct:>+7.2%} "
        f"avg_loss={m.avg_loss_pct:>+7.2%} net_pnl=${m.net_pnl:>+8.2f} "
        f"return={m.return_pct:>+7.2%} max_dd={m.max_drawdown_pct:>6.1%} "
        f"time_in_market={m.time_in_market_pct:>6.1%} "
        f"max_lose_streak={m.max_losing_streak:<3} mfe_captured={m.mfe_captured_pct:>5.1%}"
    )


def print_leg_and_position(label: str, trades: list[Trade]) -> None:
    """Report a variant at BOTH leg level (matches live closed_trades rows) and
    position level (the honest win rate), with the position exit-reason mix."""
    print_metrics(f"{label} [legs]", aggregate(trades))
    print_metrics(f"{label} [POS] ", aggregate_positions(trades))
    mix = exit_reason_mix(trades)
    if mix:
        print("    position exit mix: " + ", ".join(f"{k}={v}" for k, v in mix.items()))



# ── Fold-aware sweep runner ─────────────────────────────────────────────

async def _load_frames(tf: Timeframe, bars: int) -> dict[str, pd.DataFrame]:
    from app.data import OHLCVRepository
    return await _load(OHLCVRepository(), tf, bars)


def _risk_on_series(frames: dict[str, pd.DataFrame], sym: str) -> Optional[pd.Series]:
    btc = frames.get("BTCUSDT")
    if btc is None:
        return None
    mask = (btc["ema_50"] > btc["ema_200"])
    return mask.reindex(frames[sym].index, method="ffill").fillna(False)


def _breadth_above_ema(frames: dict[str, pd.DataFrame], span: int) -> Optional[pd.Series]:
    """Fraction of ALT symbols (excl. BTC) with close > ema_{span} on each date.

    A broad-market health gauge the single BTC-regime filter misses: BTC can sit
    above its own EMAs while most alts bleed below theirs (the 2026-10 alt
    pullback that dropped live win-rate while BTC regime stayed STRONG_BULL).
    """
    ema = f"ema_{span}"
    cols = {
        sym: (df["close"] > df[ema]).astype(float)
        for sym, df in frames.items()
        if sym != "BTCUSDT" and ema in df.columns
    }
    if not cols:
        return None
    return pd.DataFrame(cols).mean(axis=1, skipna=True)


def _breadth_risk_on_fn(span: int, threshold: float, *, with_btc: bool = False):
    """risk_on source: entries allowed only when >= threshold of alts are above
    ema_{span} (optionally ANDed with the BTC EMA50>EMA200 regime)."""
    def fn(frames: dict[str, pd.DataFrame], sym: str) -> Optional[pd.Series]:
        br = _breadth_above_ema(frames, span)
        if br is None:
            return None
        mask = (br >= threshold).reindex(frames[sym].index, method="ffill").fillna(False)
        if with_btc:
            btc = _risk_on_series(frames, sym)
            if btc is not None:
                mask = mask & btc
        return mask
    return fn


def run_sweep(
    frames: dict[str, pd.DataFrame],
    entry_fn: EntryFn,
    params: LadderParams,
    *,
    folds: int,
    market_filter: bool,
    mean_reversion_priority: bool = False,
    risk_on_fn=_risk_on_series,
) -> tuple[Metrics, list[Metrics]]:
    """Runs the simulator per-symbol per-fold; returns (pooled, per-fold)."""
    all_trades, fold_trades = _collect_sweep_trades(
        frames, entry_fn, params, folds=folds, market_filter=market_filter,
        mean_reversion_priority=mean_reversion_priority, risk_on_fn=risk_on_fn,
    )
    pooled = aggregate(all_trades)
    observation_bars = sum(len(df) for df in frames.values())
    if observation_bars:
        pooled = replace(
            pooled,
            time_in_market_pct=min(
                1.0,
                sum(t.exit_idx - t.entry_idx for t in all_trades) / observation_bars,
            ),
        )
    per_fold = []
    for fold_trade_set in fold_trades:
        metrics = aggregate(fold_trade_set)
        fold_bars = sum(len(df) // folds for df in frames.values())
        if fold_bars:
            metrics = replace(
                metrics,
                time_in_market_pct=min(
                    1.0,
                    sum(t.exit_idx - t.entry_idx for t in fold_trade_set) / fold_bars,
                ),
            )
        per_fold.append(metrics)
    return pooled, per_fold


def _collect_sweep_trades(
    frames: dict[str, pd.DataFrame], entry_fn: EntryFn, params: LadderParams,
    *, folds: int, market_filter: bool, mean_reversion_priority: bool = False,
    risk_on_fn=_risk_on_series,
) -> tuple[list[Trade], list[list[Trade]]]:
    all_trades: list[Trade] = []
    fold_trades: list[list[Trade]] = [[] for _ in range(folds)]
    for sym, df in frames.items():
        risk_on = risk_on_fn(frames, sym)
        bounds = _fold_bounds(len(df), folds)
        for f, (lo, hi) in enumerate(bounds):
            sub = df.iloc[lo:hi]
            if len(sub) < 40:
                continue
            sub_risk_on = risk_on.iloc[lo:hi] if market_filter and risk_on is not None else None
            trades = simulate_symbol(
                sym, sub, entry_fn, params, risk_on=sub_risk_on,
                mean_reversion_priority=mean_reversion_priority,
            )
            if not market_filter and risk_on is not None:
                for trade in trades:
                    trade.regime_risk_on = bool(risk_on.iloc[lo + trade.entry_idx])
            all_trades.extend(trades)
            fold_trades[f].extend(trades)
    return all_trades, fold_trades


def compare_market_filters(
    frames: dict[str, pd.DataFrame], entry_fn: EntryFn, params: LadderParams, *, folds: int,
) -> dict[str, float | int]:
    """Paired OFF/ON comparison and counterfactual P&L for filtered entries."""
    off_trades, _ = _collect_sweep_trades(frames, entry_fn, params, folds=folds, market_filter=False)
    on_trades, _ = _collect_sweep_trades(frames, entry_fn, params, folds=folds, market_filter=True)
    blocked = []
    blocked = [trade for trade in off_trades if trade.regime_risk_on is False]
    blocked_metrics = aggregate(blocked)
    return {
        "off_trades": len(off_trades),
        "on_trades": len(on_trades),
        "blocked_entries": len(blocked),
        "blocked_pnl": blocked_metrics.net_pnl,
    }


# ── Sweep entrypoints ────────────────────────────────────────────────────

async def sweep_entry(frames: dict[str, pd.DataFrame], folds: int, market_filter: bool) -> None:
    print("\n### CHANGE 1 — ENTRY STRATEGY (dip_buy vs oversold_bounce), identical risk/exits ###")
    params = default_params()
    for name, fn in (("dip_buy", entry_dip_buy), ("oversold_bounce", entry_oversold_bounce)):
        pooled, per_fold = run_sweep(frames, fn, params, folds=folds, market_filter=market_filter)
        print_metrics(name, pooled)
        for i, m in enumerate(per_fold):
            if m.n:
                print(f"    fold {i}: " + " " * 0, end="")
                print_metrics(f"  fold {i}", m)
    if "BTCUSDT" in frames:
        comparison = compare_market_filters(frames, entry_dip_buy, params, folds=folds)
        print(
            "\nPaired dip_buy gate comparison: "
            f"blocked_entries={comparison['blocked_entries']} "
            f"counterfactual_blocked_pnl=${comparison['blocked_pnl']:+.2f}"
        )


async def sweep_rsi_exit(frames: dict[str, pd.DataFrame], folds: int, market_filter: bool) -> None:
    print("\n### CHANGE 2 — RSI/MEAN-REVERSION EXIT (old vs new params), dip_buy entry ###")
    print(
        "NOTE: on daily bars evaluated in a single deterministic order, the price-based "
        "ladder (stop_loss/TP1/TP2/trailing) almost always resolves before RSI can recover "
        "enough to matter -- in production this is a real race between two independent "
        "processes (the risk loop vs. the strategy tick) on live intraday prices, which a "
        "single daily close can't reproduce. Reporting BOTH orderings as a bracket: "
        "'ladder_first' (my default ordering) and 'signal_first' (RSI-exit wins every race "
        "it's eligible for) -- the real live behavior sits somewhere between the two."
    )
    variants = {
        "old(rsi=55,min_pnl=0.0%,no_confirm)": default_params(
            mean_reversion_rsi=55, mean_reversion_min_pnl=0.0,
            mean_reversion_require_confirmation=False,
        ),
        "current(rsi=55,min_pnl=0.0%,confirm)": default_params(
            mean_reversion_rsi=55, mean_reversion_min_pnl=0.0,
            mean_reversion_require_confirmation=True,
        ),
        "proposed(rsi=60,min_pnl=0.25%,confirm)": default_params(
            mean_reversion_rsi=60, mean_reversion_min_pnl=0.0025,
            mean_reversion_require_confirmation=True,
        ),
    }
    for priority_label, priority in (("ladder_first", False), ("signal_first", True)):
        print(f"\n-- {priority_label} --")
        for name, params in variants.items():
            pooled, _ = run_sweep(
                frames, entry_dip_buy, params, folds=folds, market_filter=market_filter,
                mean_reversion_priority=priority,
            )
            print_metrics(name, pooled)


async def sweep_trailing(frames: dict[str, pd.DataFrame], folds: int, market_filter: bool) -> None:
    print("\n### CHANGE 3 — TRAILING ACTIVATION x DISTANCE grid, dip_buy entry ###")
    activations = [0.020, 0.025, 0.030]
    distances = [0.0125, 0.015, 0.020]
    print(f"(current live: activation=2.0% distance=1.0% — included for reference)")
    pooled, _ = run_sweep(
        frames, entry_dip_buy, default_params(trail_activation=0.02, trail_distance=0.01),
        folds=folds, market_filter=market_filter,
    )
    print_metrics("current(act=2.0%,dist=1.0%)", pooled)
    best = None
    for act in activations:
        for dist in distances:
            params = default_params(trail_activation=act, trail_distance=dist)
            pooled, _ = run_sweep(frames, entry_dip_buy, params, folds=folds, market_filter=market_filter)
            label = f"act={act:.1%},dist={dist:.2%}"
            print_metrics(label, pooled)
            if pooled.n >= 10 and (best is None or pooled.expectancy_pct > best[1].expectancy_pct):
                best = (label, pooled)
    if best:
        print(f"\nBest expectancy (n>=10): {best[0]} -> expectancy={best[1].expectancy_pct:+.2%} "
              f"max_dd={best[1].max_drawdown_pct:.1%} n={best[1].n}")


async def sweep_stop_stale(frames: dict[str, pd.DataFrame], folds: int, market_filter: bool) -> None:
    print("\n### CHANGE 4 — ATR-STOP CAP x STALE-EXIT (per-position), oversold_bounce entry ###")
    print(
        "Models the LIVE ATR stop (clamp(2*atr_pct, atr_min, atr_max)) and the "
        "stale_dead_money exit so the sim matches live exits. Reports BOTH leg-level "
        "(= closed_trades rows) and POSITION-level (the honest win rate). Only deploy "
        "a variant if BOTH expectancy AND profit factor beat live_baseline at POSITION "
        "level without raising max_dd."
    )
    entry_fn = entry_oversold_bounce  # live entry_strategy default
    variants = {
        "live_baseline": default_params(),
        "atr_cap_6pct": default_params(atr_stop_max=0.06),
        "atr_cap_5pct": default_params(atr_stop_max=0.05),
        "stale_off": default_params(stale_exit_enabled=False),
        "stale_3d": default_params(stale_exit_days=3),
        "stale_3d+only_up": default_params(stale_exit_days=3, stale_exit_max_pnl=0.0),
        "atr_cap_5pct+stale_3d": default_params(atr_stop_max=0.05, stale_exit_days=3),
    }
    base_pos: Optional[Metrics] = None
    for name, params in variants.items():
        all_trades, fold_trades = _collect_sweep_trades(
            frames, entry_fn, params, folds=folds, market_filter=market_filter
        )
        print()
        print_leg_and_position(name, all_trades)
        for f, ft in enumerate(fold_trades):
            fm = aggregate_positions(ft)
            if fm.n:
                print_metrics(f"    fold {f} [POS]", fm)
        pos = aggregate_positions(all_trades)
        if name == "live_baseline":
            base_pos = pos
        elif base_pos is not None and pos.n >= 10:
            better = (
                pos.expectancy_pct > base_pos.expectancy_pct
                and pos.profit_factor > base_pos.profit_factor
                and pos.max_drawdown_pct <= base_pos.max_drawdown_pct + 1e-9
            )
            verdict = "DEPLOY-CANDIDATE" if better else "no (fails expectancy/PF/dd test)"
            print(f"    vs live_baseline [POS]: {verdict}")


async def sweep_breadth(frames: dict[str, pd.DataFrame], folds: int, market_filter: bool) -> None:
    print("\n### CHANGE 5 — MARKET-BREADTH ENTRY FILTER (per-position), oversold_bounce entry ###")
    print(
        "Breadth = fraction of the ALT universe above EMA20/EMA50. The live BTC-regime "
        "filter is blind to alt pullbacks (BTC above its EMAs while alts bleed — the "
        "2026-10 drawdown). This gates entries on broad-market health instead. Reports "
        "both leg and POSITION level, per fold. Backtest only."
    )
    entry_fn = entry_oversold_bounce
    params = default_params()
    variants = {
        "btc_regime(current)": _risk_on_series,
        "breadth_ema50>=0.20(floor)": _breadth_risk_on_fn(50, 0.20),
        "breadth_ema50>=0.30(floor)": _breadth_risk_on_fn(50, 0.30),
        "breadth_ema20>=0.30(floor)": _breadth_risk_on_fn(20, 0.30),
        "btc_AND_breadth50>=0.30": _breadth_risk_on_fn(50, 0.30, with_btc=True),
    }
    base_pos: Optional[Metrics] = None
    for name, fn in variants.items():
        all_trades, fold_trades = _collect_sweep_trades(
            frames, entry_fn, params, folds=folds, market_filter=True, risk_on_fn=fn,
        )
        print()
        print_leg_and_position(name, all_trades)
        for f, ft in enumerate(fold_trades):
            fm = aggregate_positions(ft)
            if fm.n:
                print_metrics(f"    fold {f} [POS]", fm)
        pos = aggregate_positions(all_trades)
        if name == "btc_regime(current)":
            base_pos = pos
        elif base_pos is not None and pos.n >= 10:
            better = (
                pos.expectancy_pct > base_pos.expectancy_pct
                and pos.profit_factor > base_pos.profit_factor
                and pos.max_drawdown_pct <= base_pos.max_drawdown_pct + 1e-9
            )
            print(f"    vs btc_regime [POS]: {'IMPROVES' if better else 'no (exp/PF/dd test)'}")


async def main_async(which: str, tf: Timeframe, folds: int, bars: int, market_filter: bool) -> None:
    print(f"Loading up to {bars} {tf.value} bars per symbol...")
    frames = await _load_frames(tf, bars)
    if not frames:
        print("No usable symbols.")
        return
    print(f"loaded {len(frames)} symbols, {folds} folds, market_filter={market_filter}")

    if which in ("entry", "all"):
        await sweep_entry(frames, folds, market_filter)
    if which in ("rsi_exit", "all"):
        await sweep_rsi_exit(frames, folds, market_filter)
    if which in ("trailing", "all"):
        await sweep_trailing(frames, folds, market_filter)
    if which in ("stop_stale", "all"):
        await sweep_stop_stale(frames, folds, market_filter)
    if which in ("breadth", "all"):
        await sweep_breadth(frames, folds, market_filter)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("which", choices=["entry", "rsi_exit", "trailing", "stop_stale", "breadth", "all"], default="all", nargs="?")
    p.add_argument("--timeframe", default="1d", choices=[t.value for t in Timeframe])
    p.add_argument("--folds", type=int, default=3)
    p.add_argument("--bars", type=int, default=1000)
    p.add_argument("--market-filter", action=argparse.BooleanOptionalAction, default=True)
    args = p.parse_args()
    asyncio.run(main_async(args.which, Timeframe(args.timeframe), args.folds, args.bars, args.market_filter))


if __name__ == "__main__":
    main()
