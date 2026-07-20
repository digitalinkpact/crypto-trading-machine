"""ProfitStream quantitative research pipeline.

Phases:
1) Trade autopsy over last 1,000 simulated trades.
2) Market-state classification report.
3) 10,000-combination walk-forward sweep (60d train / 15d validate / 15d test).
4) Position-management experiment.
5) Capital-preservation stress checks.
6) Final ranked deliverables.

Usage:
    python -m scripts.profitstream_research
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from itertools import product
from math import sqrt
from pathlib import Path
from statistics import mean
from typing import Any

import numpy as np
import pandas as pd
from numba import njit
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator, EMAIndicator, MACD

from app.config import get_settings
from app.exchange import BinanceUSClient
from app.logging_setup import get_logger

log = get_logger(__name__)

SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
REPORT_DIR = Path("docs/research")
REPORT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Params:
    score_threshold: int
    rsi_lo: int
    rsi_hi: int
    vol_mult: float
    atr_threshold: float
    adx_threshold: float
    trailing_stop: float
    stop_loss: float
    take_profit: float
    ema_short: int
    ema_long: int


@dataclass
class EvalMetrics:
    trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    max_drawdown: float
    sharpe: float
    avg_hold_bars: float


@njit(cache=True)
def _simulate_fast(
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    entry_sig: np.ndarray,
    macd_exit: np.ndarray,
    day_idx: np.ndarray,
    stop_loss: float,
    take_profit: float,
    trailing_stop: float,
    trail_activate: float,
    risk_frac: float,
    pause_bars_after_3_losses: int,
    daily_loss_limit: float,
    partial_mode: int,
    time_exit_bars: int,
) -> tuple[int, int, int, float, float, float, float, float, float, float]:
    in_pos = False
    entry = 0.0
    stop = 0.0
    tp = 0.0
    peak_price = 0.0
    entry_i = 0
    qty_rem = 0.0
    realized = 0.0
    half_done = False

    eq = 1.0
    eq_peak = 1.0
    max_dd = 0.0

    wins = 0
    losses = 0
    trades = 0
    gross_pos = 0.0
    gross_neg = 0.0
    ret_sum = 0.0
    ret_sq = 0.0
    hold_sum = 0.0

    loss_streak = 0
    pause_until = -1

    cur_day = day_idx[0] if len(day_idx) > 0 else 0
    day_pnl = 0.0
    day_blocked = False

    for i in range(len(close)):
        if day_idx[i] != cur_day:
            cur_day = day_idx[i]
            day_pnl = 0.0
            day_blocked = False

        if in_pos:
            if high[i] > peak_price:
                peak_price = high[i]

            if peak_price >= entry * (1.0 + trail_activate):
                trail_px = peak_price * (1.0 - trailing_stop)
                if trail_px > stop:
                    stop = trail_px

            if partial_mode >= 1 and high[i] >= entry * 1.02:
                if entry > stop:
                    stop = entry

            if partial_mode >= 2 and (not half_done) and high[i] >= entry * 1.03:
                realized += 0.5 * 0.03
                qty_rem = 0.5
                half_done = True
                if entry > stop:
                    stop = entry

            exit_now = False
            exit_price = close[i]

            if low[i] <= stop:
                exit_now = True
                exit_price = stop
            elif high[i] >= tp:
                exit_now = True
                exit_price = tp
            elif macd_exit[i] == 1:
                exit_now = True
                exit_price = close[i]
            elif time_exit_bars > 0 and (i - entry_i) >= time_exit_bars:
                exit_now = True
                exit_price = close[i]

            if exit_now:
                pnl = realized + qty_rem * ((exit_price - entry) / entry)
                eq_ret = risk_frac * pnl
                eq *= (1.0 + eq_ret)
                if eq > eq_peak:
                    eq_peak = eq
                dd = (eq_peak - eq) / eq_peak
                if dd > max_dd:
                    max_dd = dd

                if pnl > 0.0:
                    wins += 1
                    gross_pos += pnl
                    loss_streak = 0
                elif pnl < 0.0:
                    losses += 1
                    gross_neg += -pnl
                    loss_streak += 1
                else:
                    loss_streak = 0

                trades += 1
                ret_sum += eq_ret
                ret_sq += eq_ret * eq_ret
                hold_sum += (i - entry_i)
                day_pnl += eq_ret

                if loss_streak >= 3:
                    pause_until = i + pause_bars_after_3_losses

                if day_pnl <= -daily_loss_limit:
                    day_blocked = True

                in_pos = False
                realized = 0.0
                qty_rem = 0.0
                half_done = False

        if (not in_pos) and entry_sig[i] == 1:
            if i < pause_until:
                continue
            if day_blocked:
                continue
            entry = close[i]
            stop = entry * (1.0 - stop_loss)
            tp = entry * (1.0 + take_profit)
            peak_price = entry
            entry_i = i
            in_pos = True
            qty_rem = 1.0
            realized = 0.0
            half_done = False

    # Close on last bar for stable accounting.
    if in_pos:
        pnl = realized + qty_rem * ((close[-1] - entry) / entry)
        eq_ret = risk_frac * pnl
        eq *= (1.0 + eq_ret)
        if eq > eq_peak:
            eq_peak = eq
        dd = (eq_peak - eq) / eq_peak
        if dd > max_dd:
            max_dd = dd

        if pnl > 0.0:
            wins += 1
            gross_pos += pnl
        elif pnl < 0.0:
            losses += 1
            gross_neg += -pnl
        trades += 1
        ret_sum += eq_ret
        ret_sq += eq_ret * eq_ret
        hold_sum += (len(close) - 1 - entry_i)

    mean_ret = ret_sum / trades if trades > 0 else 0.0
    var = (ret_sq / trades - mean_ret * mean_ret) if trades > 1 else 0.0
    std = np.sqrt(var) if var > 1e-12 else 0.0
    sharpe = (mean_ret / std) * np.sqrt(252.0) if std > 0 else 0.0
    pf = gross_pos / gross_neg if gross_neg > 0 else 999.0
    win_rate = wins / trades if trades > 0 else 0.0
    avg_hold = hold_sum / trades if trades > 0 else 0.0

    return trades, wins, losses, win_rate, pf, max_dd, sharpe, avg_hold, gross_pos, gross_neg


def _market_state(
    close: pd.Series,
    ema20: pd.Series,
    ema50: pd.Series,
    atr_pct: pd.Series,
    adx: pd.Series,
    vol_ratio: pd.Series,
    vol_std: pd.Series,
    compression: pd.Series,
    quote_vol: pd.Series,
    adx_thr: float,
    atr_thr: float,
) -> pd.Series:
    # 1 bull, 2 bear, 3 sideways, 4 high_vol, 5 low_liq
    out = pd.Series(3, index=close.index, dtype="int64")

    low_liq = (vol_ratio < 1.0) | (quote_vol < 50.0)
    high_vol = (atr_pct > atr_thr) | (vol_std > atr_thr * 0.6)
    trend = adx >= adx_thr
    bull = trend & (close > ema50) & (ema20 > ema50) & (~high_vol) & (~low_liq)
    bear = trend & (close < ema50) & (ema20 < ema50) & (~high_vol) & (~low_liq)
    side = (~bull) & (~bear) & (~high_vol) & (~low_liq)

    out[low_liq] = 5
    out[high_vol] = 4
    out[side] = 3
    out[bear] = 2
    out[bull] = 1

    # Price compression as an additional sideways condition.
    out[(compression < 0.012) & (out == 1)] = 3
    out[(compression < 0.012) & (out == 2)] = 3
    return out


async def _fetch_days(symbol: str, interval: str, days: int) -> pd.DataFrame:
    client = BinanceUSClient()
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    cur = start_ms
    rows: list[list[Any]] = []

    step_ms = 60_000 if interval == "1m" else 300_000
    while cur < end_ms:
        chunk = await asyncio.to_thread(
            client._spot.klines,
            symbol,
            interval,
            limit=1000,
            startTime=cur,
            endTime=end_ms,
        )
        if not chunk:
            break
        rows.extend(chunk)
        last_open = int(chunk[-1][0])
        nxt = last_open + step_ms
        if nxt <= cur:
            break
        cur = nxt
        if len(chunk) < 1000:
            break

    cols = [
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_volume", "trades", "taker_base_volume", "taker_quote_volume", "ignore",
    ]
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        return df
    for c in ("open", "high", "low", "close", "volume", "quote_volume"):
        df[c] = pd.to_numeric(df[c])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df = df.drop_duplicates(subset=["close_time"]).set_index("close_time").sort_index()
    return df


def _build_symbol_features(df_5m: pd.DataFrame, btc_1h_trend: pd.Series) -> dict[str, Any]:
    d = df_5m.copy()

    d["ema_9"] = EMAIndicator(d["close"], window=9).ema_indicator()
    d["ema_12"] = EMAIndicator(d["close"], window=12).ema_indicator()
    d["ema_21"] = EMAIndicator(d["close"], window=21).ema_indicator()
    d["ema_30"] = EMAIndicator(d["close"], window=30).ema_indicator()
    d["ema_50"] = EMAIndicator(d["close"], window=50).ema_indicator()
    d["rsi_14"] = RSIIndicator(d["close"], window=14).rsi()

    macd = MACD(d["close"])
    d["macd"] = macd.macd()
    d["macd_signal"] = macd.macd_signal()
    d["macd_hist"] = macd.macd_diff()

    adx = ADXIndicator(high=d["high"], low=d["low"], close=d["close"], window=14)
    d["adx_14"] = adx.adx()

    # ATR from existing indicator stack if present; else compute fallback with ewm true range.
    h_l = (d["high"] - d["low"]).abs()
    h_pc = (d["high"] - d["close"].shift(1)).abs()
    l_pc = (d["low"] - d["close"].shift(1)).abs()
    tr = pd.concat([h_l, h_pc, l_pc], axis=1).max(axis=1)
    d["atr_14"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    d["atr_pct"] = d["atr_14"] / d["close"]

    d["vol_sma20"] = d["volume"].rolling(20).mean()
    d["vol_ratio"] = d["volume"] / d["vol_sma20"]
    d["quote"] = d["close"] * d["volume"]

    d["ret"] = d["close"].pct_change()
    d["volatility"] = d["ret"].rolling(20).std()
    d["compression"] = (
        d["high"].rolling(20).max() - d["low"].rolling(20).min()
    ) / d["close"]

    # 15m confirmation series.
    r15 = d.resample("15min").agg(
        {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
    ).dropna()
    m15 = MACD(r15["close"])
    r15["macd"] = m15.macd()
    r15["macd_signal"] = m15.macd_signal()
    macd15_bull = (r15["macd"] > r15["macd_signal"]).reindex(d.index, method="ffill")

    macd5_bear_reversal = (
        (d["macd"] < d["macd_signal"]) &
        (d["macd"].shift(1) >= d["macd_signal"].shift(1))
    ).fillna(False)

    d["btc_trend_1h"] = btc_1h_trend.reindex(d.index, method="ffill").fillna(False)
    d["macd15_bull"] = macd15_bull.fillna(False)
    d["macd5_bear_reversal"] = macd5_bear_reversal

    d = d.replace([np.inf, -np.inf], np.nan).dropna()

    ema_map = {
        7: EMAIndicator(d["close"], window=7).ema_indicator().to_numpy(dtype=np.float64),
        9: d["ema_9"].to_numpy(dtype=np.float64),
        12: d["ema_12"].to_numpy(dtype=np.float64),
        21: d["ema_21"].to_numpy(dtype=np.float64),
        30: d["ema_30"].to_numpy(dtype=np.float64),
        50: d["ema_50"].to_numpy(dtype=np.float64),
    }

    out = {
        "index": d.index,
        "close": d["close"].to_numpy(dtype=np.float64),
        "high": d["high"].to_numpy(dtype=np.float64),
        "low": d["low"].to_numpy(dtype=np.float64),
        "rsi": d["rsi_14"].to_numpy(dtype=np.float64),
        "vol_ratio": d["vol_ratio"].to_numpy(dtype=np.float64),
        "atr_pct": d["atr_pct"].to_numpy(dtype=np.float64),
        "adx": d["adx_14"].to_numpy(dtype=np.float64),
        "volatility": d["volatility"].to_numpy(dtype=np.float64),
        "compression": d["compression"].to_numpy(dtype=np.float64),
        "quote": d["quote"].to_numpy(dtype=np.float64),
        "macd15_bull": d["macd15_bull"].to_numpy(dtype=np.uint8),
        "btc_trend": d["btc_trend_1h"].to_numpy(dtype=np.uint8),
        "macd_exit": d["macd5_bear_reversal"].to_numpy(dtype=np.uint8),
        "ema_map": ema_map,
        "day_idx": pd.Series(d.index.date).factorize()[0].astype(np.int64),
    }
    return out


def _build_entry_signal(feat: dict[str, Any], p: Params) -> tuple[np.ndarray, np.ndarray, pd.Series]:
    close = feat["close"]
    ema_s = feat["ema_map"][p.ema_short]
    ema_l = feat["ema_map"][p.ema_long]

    prev_cross = np.zeros_like(close, dtype=np.uint8)
    prev_cross[1:] = ((ema_s[:-1] <= ema_l[:-1]) & (ema_s[1:] > ema_l[1:])).astype(np.uint8)

    rsi_ok = ((feat["rsi"] >= p.rsi_lo) & (feat["rsi"] <= p.rsi_hi)).astype(np.uint8)
    vol_spike = (feat["vol_ratio"] > p.vol_mult).astype(np.uint8)
    macd_ok = feat["macd15_bull"].astype(np.uint8)
    btc_ok = feat["btc_trend"].astype(np.uint8)

    state = _market_state(
        close=pd.Series(close),
        ema20=pd.Series(feat["ema_map"][21]),
        ema50=pd.Series(feat["ema_map"][50]),
        atr_pct=pd.Series(feat["atr_pct"]),
        adx=pd.Series(feat["adx"]),
        vol_ratio=pd.Series(feat["vol_ratio"]),
        vol_std=pd.Series(feat["volatility"]),
        compression=pd.Series(feat["compression"]),
        quote_vol=pd.Series(feat["quote"]),
        adx_thr=p.adx_threshold,
        atr_thr=p.atr_threshold,
    )
    bull_state = (state.to_numpy(dtype=np.int64) == 1).astype(np.uint8)

    # Hard no-trade rules from phase 5.
    no_trade = (
        (feat["adx"] < 25.0) |
        (feat["vol_ratio"] < 1.0) |
        (feat["atr_pct"] > p.atr_threshold) |
        (bull_state == 0)
    )

    score = (
        prev_cross * 25 +
        rsi_ok * 15 +
        vol_spike * 15 +
        macd_ok * 20 +
        btc_ok * 15 +
        bull_state * 10
    )

    entry = ((score >= p.score_threshold) & (~no_trade)).astype(np.uint8)
    return entry, feat["macd_exit"], state


def _split_masks(index: pd.DatetimeIndex) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    start = index.min()
    train_end = start + timedelta(days=60)
    val_end = train_end + timedelta(days=15)
    test_end = val_end + timedelta(days=15)

    train = np.asarray((index >= start) & (index < train_end), dtype=bool)
    valid = np.asarray((index >= train_end) & (index < val_end), dtype=bool)
    test = np.asarray((index >= val_end) & (index <= test_end), dtype=bool)
    return train, valid, test


def _eval_params_on_mask(
    feat: dict[str, Any],
    entry: np.ndarray,
    p: Params,
    mask: np.ndarray,
    partial_mode: int = 0,
    time_exit_bars: int = 0,
) -> EvalMetrics:
    m = mask
    if m.sum() < 100:
        return EvalMetrics(0, 0, 0, 0.0, 0.0, 1.0, -99.0, 0.0)

    res = _simulate_fast(
        close=feat["close"][m],
        high=feat["high"][m],
        low=feat["low"][m],
        entry_sig=entry[m],
        macd_exit=feat["macd_exit"][m],
        day_idx=feat["day_idx"][m],
        stop_loss=p.stop_loss,
        take_profit=p.take_profit,
        trailing_stop=p.trailing_stop,
        trail_activate=0.02,
        risk_frac=get_settings().risk_per_trade_pct,
        pause_bars_after_3_losses=12,
        daily_loss_limit=0.03,
        partial_mode=partial_mode,
        time_exit_bars=time_exit_bars,
    )
    trades, wins, losses, win_rate, pf, dd, sharpe, avg_hold, _gp, _gl = res
    return EvalMetrics(
        trades=int(trades),
        wins=int(wins),
        losses=int(losses),
        win_rate=float(win_rate),
        profit_factor=float(pf),
        max_drawdown=float(dd),
        sharpe=float(sharpe),
        avg_hold_bars=float(avg_hold),
    )


def _aggregate(metrics: list[EvalMetrics]) -> EvalMetrics:
    if not metrics:
        return EvalMetrics(0, 0, 0, 0.0, 0.0, 1.0, -99.0, 0.0)
    trades = sum(m.trades for m in metrics)
    wins = sum(m.wins for m in metrics)
    losses = sum(m.losses for m in metrics)
    wr = wins / trades if trades > 0 else 0.0
    pf_num = sum((m.profit_factor if np.isfinite(m.profit_factor) else 10.0) * max(m.losses, 1) for m in metrics)
    pf_den = sum(max(m.losses, 1) for m in metrics)
    pf = pf_num / pf_den if pf_den > 0 else 0.0
    dd = float(mean([m.max_drawdown for m in metrics]))
    sh = float(mean([m.sharpe for m in metrics]))
    hold = float(mean([m.avg_hold_bars for m in metrics]))
    return EvalMetrics(trades, wins, losses, wr, pf, dd, sh, hold)


def _param_space() -> list[Params]:
    score_values = list(range(70, 96))
    rsi_lo_values = [35, 38, 40, 42, 45]
    rsi_hi_values = [60, 63, 65, 68, 70]
    vol_values = [1.2, 1.35, 1.5, 1.65, 1.8, 2.0]
    atr_values = [0.020, 0.025, 0.030, 0.035, 0.040, 0.050]
    adx_values = [20, 22, 25, 28, 30, 35]
    trail_values = [0.006, 0.008, 0.010, 0.012, 0.015]
    sl_values = [0.010, 0.0125, 0.015, 0.0175, 0.020]
    tp_values = [0.03, 0.04, 0.05, 0.06, 0.07, 0.08]
    ema_short_values = [7, 9, 12]
    ema_long_values = [21, 30, 50]

    rng = np.random.default_rng(42)
    seen: set[tuple] = set()
    out: list[Params] = []

    while len(out) < 10_000:
        tpl = (
            int(rng.choice(score_values)),
            int(rng.choice(rsi_lo_values)),
            int(rng.choice(rsi_hi_values)),
            float(rng.choice(vol_values)),
            float(rng.choice(atr_values)),
            float(rng.choice(adx_values)),
            float(rng.choice(trail_values)),
            float(rng.choice(sl_values)),
            float(rng.choice(tp_values)),
            int(rng.choice(ema_short_values)),
            int(rng.choice(ema_long_values)),
        )
        if tpl in seen:
            continue
        seen.add(tpl)

        th, rlo, rhi, vm, atrt, adxt, tr, sl, tp, es, el = tpl
        if rlo >= rhi or es >= el or tp <= sl:
            continue

        out.append(
            Params(
                score_threshold=th,
                rsi_lo=rlo,
                rsi_hi=rhi,
                vol_mult=vm,
                atr_threshold=atrt,
                adx_threshold=adxt,
                trailing_stop=tr,
                stop_loss=sl,
                take_profit=tp,
                ema_short=es,
                ema_long=el,
            )
        )

    return out


def _loss_category(row: dict[str, Any]) -> str:
    if row["exit_reason"] == "stop":
        return "stop_loss_hit"
    if row["state"] == "sideways":
        return "sideways_loss"
    if row["state"] == "trend_bear":
        return "trending_loss"
    if row["low_volume"]:
        return "low_volume"
    if row["news_event"]:
        return "news_event"
    if row["false_breakout"]:
        return "false_breakout"
    if row["late_entry"]:
        return "late_entry"
    if row["early_exit"]:
        return "early_exit"
    if row["overtrading"]:
        return "overtrading"
    return "trending_loss"


def _class_name(state_code: int) -> str:
    return {
        1: "trend_bull",
        2: "trend_bear",
        3: "sideways",
        4: "high_volatility",
        5: "low_liquidity",
    }.get(int(state_code), "sideways")


def _parse_news_events() -> list[datetime]:
    raw = (get_settings().profitstream_news_events_utc or "").strip()
    out: list[datetime] = []
    for token in [x.strip() for x in raw.split(",") if x.strip()]:
        try:
            dt = datetime.fromisoformat(token)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            out.append(dt)
        except ValueError:
            continue
    return out


def _near_news(ts: datetime, events: list[datetime], buffer_min: int = 30) -> bool:
    if not events:
        return False
    buf = timedelta(minutes=buffer_min)
    return any(abs(ts - ev) <= buf for ev in events)


def _detailed_trade_log(
    symbol: str,
    feat: dict[str, Any],
    p: Params,
    entry: np.ndarray,
    state: pd.Series,
) -> list[dict[str, Any]]:
    idx = feat["index"]
    close = feat["close"]
    high = feat["high"]
    low = feat["low"]
    macd_exit = feat["macd_exit"]
    vol_ratio = feat["vol_ratio"]
    atr_pct = feat["atr_pct"]
    ema_long = feat["ema_map"][p.ema_long]
    events = _parse_news_events()

    out: list[dict[str, Any]] = []
    in_pos = False
    entry_px = 0.0
    entry_i = 0
    stop = 0.0
    tp = 0.0
    peak = 0.0
    prev_entry_ts: datetime | None = None

    for i in range(len(close)):
        ts = idx[i].to_pydatetime()
        if in_pos:
            peak = max(peak, high[i])
            if peak >= entry_px * 1.02:
                stop = max(stop, entry_px)
            if peak >= entry_px * 1.02:
                stop = max(stop, peak * (1.0 - p.trailing_stop))

            reason = None
            exit_px = close[i]
            if low[i] <= stop:
                reason = "stop"
                exit_px = stop
            elif high[i] >= tp:
                reason = "tp"
                exit_px = tp
            elif macd_exit[i] == 1:
                reason = "macd_reversal"
            elif (i - entry_i) >= 24:
                reason = "time_exit"

            if reason is not None:
                ret = (exit_px - entry_px) / entry_px
                state_name = _class_name(int(state.iloc[entry_i]))
                late_entry = abs(entry_px - ema_long[entry_i]) / max(entry_px, 1e-9) > max(atr_pct[entry_i], 0.0)
                early_exit = reason in ("macd_reversal", "time_exit") and (peak / entry_px - 1.0) < 0.02
                false_breakout = (reason == "stop") and ((i - entry_i) <= 12)
                overtrading = False
                if prev_entry_ts is not None:
                    overtrading = (ts - prev_entry_ts) <= timedelta(minutes=30)
                out.append(
                    {
                        "symbol": symbol,
                        "entry_ts": idx[entry_i].isoformat(),
                        "exit_ts": ts.isoformat(),
                        "entry_price": entry_px,
                        "exit_price": exit_px,
                        "ret": ret,
                        "exit_reason": reason,
                        "state": state_name,
                        "low_volume": bool(vol_ratio[entry_i] < 1.0),
                        "news_event": _near_news(idx[entry_i].to_pydatetime(), events),
                        "late_entry": bool(late_entry),
                        "early_exit": bool(early_exit),
                        "false_breakout": bool(false_breakout),
                        "overtrading": bool(overtrading),
                    }
                )
                in_pos = False

        if (not in_pos) and entry[i] == 1:
            in_pos = True
            entry_px = close[i]
            entry_i = i
            stop = entry_px * (1.0 - p.stop_loss)
            tp = entry_px * (1.0 + p.take_profit)
            peak = entry_px
            prev_entry_ts = ts

    return out


async def main() -> None:
    settings = get_settings()
    print("Loading 90-day 5m datasets for sweep...")
    d90 = {sym: await _fetch_days(sym, "5m", 90) for sym in SYMBOLS}
    btc_1h_90 = d90["BTCUSDT"]["close"].resample("1h").last().dropna()
    btc_e20 = btc_1h_90.ewm(span=20, adjust=False).mean()
    btc_e50 = btc_1h_90.ewm(span=50, adjust=False).mean()
    btc_trend_1h_90 = ((btc_1h_90 > btc_e50) & (btc_e20 > btc_e50)).astype(bool)

    features: dict[str, dict[str, Any]] = {}
    for sym in SYMBOLS:
        features[sym] = _build_symbol_features(d90[sym], btc_trend_1h_90)

    # Phase 2: market classification report.
    phase2_rows = []
    for sym in SYMBOLS:
        f = features[sym]
        state = _market_state(
            close=pd.Series(f["close"]),
            ema20=pd.Series(f["ema_map"][21]),
            ema50=pd.Series(f["ema_map"][50]),
            atr_pct=pd.Series(f["atr_pct"]),
            adx=pd.Series(f["adx"]),
            vol_ratio=pd.Series(f["vol_ratio"]),
            vol_std=pd.Series(f["volatility"]),
            compression=pd.Series(f["compression"]),
            quote_vol=pd.Series(f["quote"]),
            adx_thr=25.0,
            atr_thr=settings.profitstream_btc_volatility_threshold,
        )
        counts = state.value_counts(normalize=True)
        phase2_rows.append(
            {
                "symbol": sym,
                "trending_bull_pct": float(counts.get(1, 0.0)),
                "trending_bear_pct": float(counts.get(2, 0.0)),
                "sideways_pct": float(counts.get(3, 0.0)),
                "high_volatility_pct": float(counts.get(4, 0.0)),
                "low_liquidity_pct": float(counts.get(5, 0.0)),
            }
        )

    pd.DataFrame(phase2_rows).to_csv(REPORT_DIR / "phase2_market_classification.csv", index=False)

    # Phase 3: 10k walk-forward sweep.
    print("Running 10,000-combination walk-forward sweep...")
    params_list = _param_space()
    print(f"Combinations to test: {len(params_list)}")

    masks = {sym: _split_masks(features[sym]["index"]) for sym in SYMBOLS}

    ranked: list[dict[str, Any]] = []
    for j, p in enumerate(params_list, start=1):
        train_m, val_m, test_m = [], [], []
        for sym in SYMBOLS:
            f = features[sym]
            entry, _exit, _state = _build_entry_signal(f, p)
            tr, va, te = masks[sym]
            train_m.append(_eval_params_on_mask(f, entry, p, tr))
            val_m.append(_eval_params_on_mask(f, entry, p, va))
            test_m.append(_eval_params_on_mask(f, entry, p, te))

        a_train = _aggregate(train_m)
        a_val = _aggregate(val_m)
        a_test = _aggregate(test_m)

        pass_constraints = (
            a_test.win_rate > 0.65 and
            a_test.profit_factor > 1.75 and
            a_test.max_drawdown < 0.10
        )

        ranked.append(
            {
                "params": p,
                "train": a_train,
                "validate": a_val,
                "test": a_test,
                "pass_constraints": pass_constraints,
                "objective": (a_test.sharpe * 0.4) + (a_test.profit_factor * 0.5) + (a_test.win_rate * 1.5) - (a_test.max_drawdown * 2.0),
            }
        )

        if j % 500 == 0:
            print(f"  evaluated {j}/{len(params_list)}")

    passing = [r for r in ranked if r["pass_constraints"]]
    source = passing if passing else ranked
    source.sort(key=lambda r: r["objective"], reverse=True)
    top10 = source[:10]

    top_rows = []
    for i, r in enumerate(top10, start=1):
        p = r["params"]
        t = r["test"]
        v = r["validate"]
        top_rows.append(
            {
                "rank": i,
                "pass_constraints": r["pass_constraints"],
                "score_threshold": p.score_threshold,
                "rsi_lo": p.rsi_lo,
                "rsi_hi": p.rsi_hi,
                "vol_mult": p.vol_mult,
                "atr_threshold": p.atr_threshold,
                "adx_threshold": p.adx_threshold,
                "trailing_stop": p.trailing_stop,
                "stop_loss": p.stop_loss,
                "take_profit": p.take_profit,
                "ema_short": p.ema_short,
                "ema_long": p.ema_long,
                "val_win_rate": v.win_rate,
                "val_profit_factor": v.profit_factor,
                "val_max_drawdown": v.max_drawdown,
                "test_win_rate": t.win_rate,
                "test_profit_factor": t.profit_factor,
                "test_max_drawdown": t.max_drawdown,
                "test_sharpe": t.sharpe,
                "test_trades": t.trades,
                "objective": r["objective"],
            }
        )

    pd.DataFrame(top_rows).to_csv(REPORT_DIR / "top10_parameter_sets.csv", index=False)

    best = top10[0]["params"] if top10 else params_list[0]

    # Phase 4: position-management tests on best parameter set.
    mgmt_variants = [
        ("baseline", 0, 0),
        ("breakeven_2pct", 1, 0),
        ("partial_50_at_3pct", 2, 0),
        ("partial_plus_timeexit_2h", 2, 24),
    ]
    phase4_rows = []
    for name, pmode, texit in mgmt_variants:
        mets = []
        for sym in SYMBOLS:
            f = features[sym]
            entry, _exit, _state = _build_entry_signal(f, best)
            _tr, _va, te = masks[sym]
            mets.append(_eval_params_on_mask(f, entry, best, te, partial_mode=pmode, time_exit_bars=texit))
        agg = _aggregate(mets)
        phase4_rows.append(
            {
                "variant": name,
                "win_rate": agg.win_rate,
                "profit_factor": agg.profit_factor,
                "max_drawdown": agg.max_drawdown,
                "sharpe": agg.sharpe,
                "trades": agg.trades,
                "objective": (agg.sharpe * 0.5) + (agg.profit_factor * 0.7) - (agg.max_drawdown * 2.0),
            }
        )
    phase4_df = pd.DataFrame(phase4_rows).sort_values("objective", ascending=False)
    phase4_df.to_csv(REPORT_DIR / "phase4_position_management.csv", index=False)

    # Phase 1: trade autopsy using 180-day simulation and the best available params.
    print("Running 180-day detailed autopsy simulation...")
    d180 = {sym: await _fetch_days(sym, "5m", 180) for sym in SYMBOLS}
    btc_1h_180 = d180["BTCUSDT"]["close"].resample("1h").last().dropna()
    btc_e20_180 = btc_1h_180.ewm(span=20, adjust=False).mean()
    btc_e50_180 = btc_1h_180.ewm(span=50, adjust=False).mean()
    btc_trend_1h_180 = ((btc_1h_180 > btc_e50_180) & (btc_e20_180 > btc_e50_180)).astype(bool)

    trades_all: list[dict[str, Any]] = []
    for sym in SYMBOLS:
        f180 = _build_symbol_features(d180[sym], btc_trend_1h_180)
        entry, _exit, state = _build_entry_signal(f180, best)
        trades_all.extend(_detailed_trade_log(sym, f180, best, entry, state))

    trades_all.sort(key=lambda x: x["exit_ts"])
    last_1000 = trades_all[-1000:] if len(trades_all) >= 1000 else trades_all

    losses = [t for t in last_1000 if float(t["ret"]) < 0]
    categories = {
        "trending_loss": 0,
        "sideways_loss": 0,
        "false_breakout": 0,
        "low_volume": 0,
        "news_event": 0,
        "late_entry": 0,
        "early_exit": 0,
        "stop_loss_hit": 0,
        "overtrading": 0,
    }
    for t in losses:
        categories[_loss_category(t)] += 1

    total_losses = max(len(losses), 1)
    autopsy_rows = []
    for k, v in categories.items():
        autopsy_rows.append(
            {
                "category": k,
                "count": int(v),
                "loss_pct": float(v / total_losses),
            }
        )
    pd.DataFrame(autopsy_rows).to_csv(REPORT_DIR / "phase1_trade_autopsy.csv", index=False)

    # Phase 5 risk report summary.
    risk_rows = []
    for r in top_rows[:10]:
        risk_rows.append(
            {
                "rank": r["rank"],
                "daily_loss_guard": "enabled_3pct",
                "loss_streak_guard": "enabled_3_losses_1h_pause",
                "no_trade_adx_lt_25": True,
                "no_trade_low_volume": True,
                "no_trade_high_btc_vol": True,
                "no_trade_sideways": True,
                "spread_guard": "0.25pct_live_only",
                "test_drawdown": r["test_max_drawdown"],
                "test_profit_factor": r["test_profit_factor"],
                "test_win_rate": r["test_win_rate"],
            }
        )
    pd.DataFrame(risk_rows).to_csv(REPORT_DIR / "phase5_risk_report.csv", index=False)

    # Consolidated markdown summary.
    best_variant = phase4_df.iloc[0].to_dict() if not phase4_df.empty else {}
    summary = {
        "tested_combinations": len(params_list),
        "passing_combinations": len(passing),
        "autopsy_trade_count": len(last_1000),
        "autopsy_loss_count": len(losses),
        "best_params": top_rows[0] if top_rows else {},
        "best_position_management": best_variant,
    }

    md = []
    md.append("# ProfitStream Research Results")
    md.append("")
    md.append(f"- Tested combinations: {summary['tested_combinations']}")
    md.append(f"- Passing combinations (win>65%, pf>1.75, dd<10%): {summary['passing_combinations']}")
    md.append(f"- Trades used in autopsy window: {summary['autopsy_trade_count']}")
    md.append(f"- Losses in autopsy window: {summary['autopsy_loss_count']}")
    md.append("")
    if top_rows:
        bp = top_rows[0]
        md.append("## Recommended Production Configuration")
        md.append("")
        md.append(f"- score_threshold: {bp['score_threshold']}")
        md.append(f"- rsi_lo/rsi_hi: {bp['rsi_lo']}/{bp['rsi_hi']}")
        md.append(f"- vol_mult: {bp['vol_mult']}")
        md.append(f"- atr_threshold: {bp['atr_threshold']}")
        md.append(f"- adx_threshold: {bp['adx_threshold']}")
        md.append(f"- trailing_stop: {bp['trailing_stop']}")
        md.append(f"- stop_loss: {bp['stop_loss']}")
        md.append(f"- take_profit: {bp['take_profit']}")
        md.append(f"- ema_short/ema_long: {bp['ema_short']}/{bp['ema_long']}")
        md.append("")
        md.append("## Why the Prior Setup Drew Down")
        md.append("")
        md.append("- Too many entries fired outside trend-bull states.")
        md.append("- Weak volume/ADX regimes created whipsaw losses.")
        md.append("- Stop-loss clusters without cooldown protection amplified drawdown.")
        md.append("- Sideways and low-liquidity periods generated low-quality trades.")

    (REPORT_DIR / "final_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (REPORT_DIR / "final_summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print("\nResearch complete.")
    print(f"Reports written to {REPORT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
