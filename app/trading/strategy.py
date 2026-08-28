"""ProfitStream strategy surface.

The original low-timeframe momentum stack was negative out of sample on this
repository's walk-forward harness. ProfitStream uses daily dip-buy mean
reversion plus trend pullbacks, with BTC trend retained as soft score context.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import pandas as pd

from app.config import get_settings
from app.exchange import BinanceUSClient
from app.exchange.orderbook import analyze_order_book
from app.logging_setup import get_logger
from app.regime.btc_regime import compute_btc_regime_score
from app.signals import SignalAction
from app.storage import storage
from app.ta import add_indicators

log = get_logger(__name__)


@dataclass
class StrategyDecision:
    symbol: str
    action: SignalAction
    score: int
    reasons: list[str]
    indicators: dict[str, Any]


@dataclass
class EntryCandidate:
    ready: bool
    score: int
    detail: dict[str, Any]


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


class ProfitStreamStrategy:
    """Evidence-led dip-buy strategy with explainable rejects."""

    def __init__(self, client: Optional[BinanceUSClient] = None) -> None:
        self._client = client or BinanceUSClient()

    async def analyze_symbol(
        self,
        symbol: str,
        *,
        mode: str,
        btc_1d: Optional[pd.DataFrame] = None,
    ) -> StrategyDecision:
        reasons: list[str] = []
        indicators: dict[str, Any] = {"symbol": symbol}
        s = get_settings()

        try:
            df_1d = await self._candles(symbol, "1d", 320)
            if btc_1d is None:
                btc_1d = await self._candles("BTCUSDT", "1d", 320)
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"data_unavailable:{exc}")
            return StrategyDecision(symbol, SignalAction.HOLD, 0, reasons, indicators)

        if min(len(df_1d), len(btc_1d)) < 60:
            reasons.append("insufficient_history")
            return StrategyDecision(symbol, SignalAction.HOLD, 0, reasons, indicators)

        held = self._is_held(symbol, mode)
        exit_ready, exit_rsi = self._mean_reversion_exit(df_1d)
        if s.entry_strategy == "oversold_bounce":
            dip_ready, dip_rsi = self._oversold_bounce_setup(df_1d)
        else:
            dip_ready, dip_rsi = self._dip_buy_setup(df_1d)
        pullback_ready, pullback_rsi, pullback_dist_pct = self._pullback_setup(df_1d)
        btc_risk_on, btc_ema50, btc_ema200 = self._btc_risk_on(btc_1d)
        btc_regime = compute_btc_regime_score(btc_1d)
        daily_quote = self._latest_quote_volume(df_1d)
        extended, extension_pct = self._dip_too_extended(df_1d)
        # Candidate entry types under evaluation (2026-08-21 walk-forward) are
        # logged for forensic comparison. These scored candidates remain
        # observability-only; the live pullback path uses `_pullback_setup`.
        candidates = {
            name: {"ready": c.ready, "score": c.score, **c.detail}
            for name, c in self._score_entry_candidates(df_1d).items()
        }

        indicators.update(
            {
                "rsi_1d": dip_rsi,
                "pullback_rsi_1d": pullback_rsi,
                "pullback_ema20_dist_pct": pullback_dist_pct,
                "pullback_ready": pullback_ready,
                "exit_rsi_1d": exit_rsi,
                "close_1d": float(df_1d.iloc[-1]["close"]),
                "bb_lower_1d": float(df_1d.iloc[-1]["bb_lower"]),
                "bb_mid_1d": float(df_1d.iloc[-1]["bb_mid"]),
                "quote_volume_1d": daily_quote,
                "btc_ema50_1d": btc_ema50,
                "btc_ema200_1d": btc_ema200,
                "btc_risk_on_1d": btc_risk_on,
                "btc_regime_score": btc_regime.score,
                "btc_regime_label": btc_regime.label,
                "ema20_extension_pct": extension_pct,
                "entry_candidates": candidates,
            }
        )

        if held and exit_ready:
            # Evidence (scripts/daily_forensic_report.py against real live
            # trade history, both a 90-day and a 5-day post-deploy window):
            # this RSI-recovery exit was by FAR the worst-performing exit
            # path — 137 trades/8.8% win-rate/-$31.64 over 90d, and 31
            # trades/3.2% win-rate/-$7.61 in just the 5 days after the
            # regime-gating deploy — dwarfing stop_loss's own -$13.85 and
            # completely eclipsing take_profit/trailing_stop's positive
            # totals. RSI ticking back above the recovery threshold only
            # means "no longer deeply oversold", not "the trade worked", so
            # this exit now requires ALL of: breakeven-or-better PnL, the
            # position not already having run into TP/trailing territory,
            # and (when enabled) an independent momentum or price
            # confirmation that the move is actually rolling over — not RSI
            # alone.
            held_pos = self._held_position(symbol, mode)
            entry_price = float((held_pos or {}).get("entry_price") or 0.0)
            signal_close = float(df_1d.iloc[-1]["close"])
            signal_pnl_pct = (
                (signal_close - entry_price) / entry_price if entry_price > 0 else 0.0
            )
            try:
                executable_price = float(await self._client.ticker_price(symbol))
            except Exception as exc:  # noqa: BLE001
                reasons.append(f"mean_reversion_exit_price_unavailable:{exc}")
                executable_price = 0.0
            held_pnl_pct = (
                (executable_price - entry_price) / entry_price
                if entry_price > 0 and executable_price > 0
                else None
            )
            indicators["held_signal_pnl_pct"] = signal_pnl_pct
            indicators["held_pnl_pct"] = held_pnl_pct

            if held_pnl_pct is None:
                reasons.append("mean_reversion_exit_suppressed_without_live_price")
            elif held_pnl_pct < s.mean_reversion_exit_min_pnl_pct:
                reasons.append(
                    f"mean_reversion_exit_suppressed_at_loss:pnl={held_pnl_pct:.2%}"
                    f"<{s.mean_reversion_exit_min_pnl_pct:.2%}"
                )
            elif (
                s.mean_reversion_exit_defer_to_risk_ladder
                and held_pnl_pct >= float(s.trailing_activation_pct)
            ):
                # Already running into "let it ride" territory — the trailing
                # stop is armed (or about to be) and TP1/TP2 are better judges
                # of when to bank a real winner than a bare technical signal.
                reasons.append(
                    f"mean_reversion_exit_deferred_to_risk_ladder:pnl={held_pnl_pct:.2%}"
                    f">={float(s.trailing_activation_pct):.2%}"
                )
            else:
                momentum_confirmed = self._momentum_deteriorating(df_1d)
                price_confirmed = self._bearish_price_confirmation(df_1d)
                indicators["mean_reversion_momentum_confirmed"] = momentum_confirmed
                indicators["mean_reversion_price_confirmed"] = price_confirmed
                need_momentum = s.mean_reversion_exit_require_momentum_confirmation
                need_price = s.mean_reversion_exit_require_price_confirmation
                if need_momentum or need_price:
                    confirmed = (need_momentum and momentum_confirmed) or (need_price and price_confirmed)
                else:
                    confirmed = True  # no confirmation configured -> plain RSI+PnL gate
                if confirmed:
                    if need_momentum and momentum_confirmed:
                        exit_reason = "mean_reversion_rsi_momentum"
                    elif need_price and price_confirmed:
                        exit_reason = "mean_reversion_rsi_price"
                    else:
                        exit_reason = "mean_reversion_rsi"
                    indicators["decision"] = "sell_mean_reversion_exit"
                    indicators["exit_reason"] = exit_reason
                    return StrategyDecision(
                        symbol,
                        SignalAction.SELL,
                        _clamp(round(exit_rsi), 0, 100),
                        reasons,
                        indicators,
                    )
                reasons.append("mean_reversion_exit_awaiting_confirmation")

        filt_ok = True

        if daily_quote < s.profitstream_low_volume_quote_min:
            filt_ok = False
            reasons.append(
                f"low_volume:{daily_quote:.2f}<{s.profitstream_low_volume_quote_min:.2f}"
            )

        if extended:
            filt_ok = False
            reasons.append(
                f"extension_too_deep_falling_knife:{extension_pct:.2%}>{s.max_dip_extension_pct:.2%}"
            )

        near_news, next_news = self._near_news_event()
        indicators["near_news"] = near_news
        if near_news:
            filt_ok = False
            reasons.append(f"news_blackout:{next_news}")

        spread_pct = await self._spread_pct(symbol)
        indicators["spread_pct"] = spread_pct
        max_spread_pct = 0.01 if pullback_ready and not dip_ready else 0.0025
        if spread_pct is not None and spread_pct > max_spread_pct:
            filt_ok = False
            reasons.append(f"spread_wide:{spread_pct:.4%}>{max_spread_pct:.4%}")

        btc_score_penalty = 0
        if not btc_risk_on:
            # Keep BTC trend context visible, but do not hard-reject entries.
            # Apply only a small score penalty so trades can still flow.
            btc_score_penalty = 10
            reasons.append("btc_trend_not_aligned_soft")

        score = 0
        score += 70 if dip_ready else 0
        score += 60 if pullback_ready else 0
        score += 20 if btc_risk_on else 0
        score += 10 if filt_ok else 0
        score -= btc_score_penalty
        score = _clamp(score, 0, 100)

        if not dip_ready:
            rsi_bar = s.oversold_bounce_rsi_max if s.entry_strategy == "oversold_bounce" else 45
            bb_mult = s.oversold_bounce_bb_multiplier if s.entry_strategy == "oversold_bounce" else 1.0
            if dip_rsi >= rsi_bar:
                reasons.append("rsi_not_oversold")
            if float(df_1d.iloc[-1]["close"]) > float(df_1d.iloc[-1]["bb_lower"]) * bb_mult:
                reasons.append("close_above_lower_band")

        if not pullback_ready:
            out = df_1d.dropna()
            if not out.empty:
                last = out.iloc[-1]
                close = float(last["close"])
                ema20 = float(last["ema_20"])
                ema50 = float(last["ema_50"])
                rsi_val = float(last["rsi_14"])
                if not (35 <= rsi_val <= 70):
                    reasons.append("pullback_rsi_out_of_range")
                if not (close > ema20 and close > ema50):
                    reasons.append("pullback_below_ema_structure")
                if not (ema20 > 0 and close >= ema20 and ((close - ema20) / ema20) <= 0.07):
                    reasons.append("pullback_too_far_from_ema20")

        if held:
            reasons.append("position_already_open")
            return StrategyDecision(symbol, SignalAction.HOLD, score, reasons, indicators)

        entry_ready = dip_ready or pullback_ready
        if entry_ready and filt_ok:
            indicators["decision"] = "buy"
            indicators["entry_strategy"] = (s.entry_strategy if dip_ready else "pullback")
            return StrategyDecision(symbol, SignalAction.BUY, score, reasons, indicators)

        indicators["decision"] = "hold"
        return StrategyDecision(symbol, SignalAction.HOLD, score, reasons, indicators)

    async def _candles(self, symbol: str, interval: str, limit: int) -> pd.DataFrame:
        df = await self._client.klines(symbol, interval, limit=limit)
        if "ema_20" not in df.columns:
            df = add_indicators(df)
        df = df.dropna()
        # Binance's klines endpoint includes the still-forming current bar. On
        # Binance.US's thin books that bar's volume is usually near/exactly 0
        # seconds after it opens, permanently failing the low-volume filter, and
        # its OHLC keeps shifting mid-bar (flickering EMA/MACD crosses). Drop it
        # so every indicator is computed off fully closed candles only.
        if not df.empty and df.index[-1] > pd.Timestamp.now(tz="UTC"):
            df = df.iloc[:-1]
        return df

    def _dip_buy_setup(self, df: pd.DataFrame) -> tuple[bool, float]:
        out = df.dropna()
        if out.empty or "rsi_14" not in out.columns or "bb_lower" not in out.columns:
            return False, 0.0
        last = out.iloc[-1]
        rsi = float(last["rsi_14"])
        close = float(last["close"])
        bb_lower = float(last["bb_lower"])
        return bool(rsi < 30 and close <= bb_lower), rsi

    def _oversold_bounce_setup(self, df: pd.DataFrame) -> tuple[bool, float]:
        """Looser dip-buy (Fix 5 A/B candidate): also requires price already
        off its 5-day low, i.e. not still in free-fall. Selected in place of
        `_dip_buy_setup` when `entry_strategy=oversold_bounce`."""
        s = get_settings()
        out = df.dropna()
        if out.empty or "rsi_14" not in out.columns or "bb_lower" not in out.columns:
            return False, 0.0
        last = out.iloc[-1]
        rsi = float(last["rsi_14"])
        close = float(last["close"])
        bb_lower = float(last["bb_lower"])
        low5 = float(out["low"].tail(5).min()) if len(out) >= 5 else close
        bounced = low5 > 0 and close > low5 * (1 + s.oversold_bounce_min_bounce_pct)
        ready = bool(
            rsi < s.oversold_bounce_rsi_max
            and close < bb_lower * s.oversold_bounce_bb_multiplier
            and bounced
        )
        return ready, rsi

    def _dip_too_extended(self, df: pd.DataFrame) -> tuple[bool, float]:
        """Anti-chase / falling-knife guard.

        A dip-buy is a healthy pullback when price is modestly below its
        EMA20; once the distance passes `max_dip_extension_pct` it's more
        likely a capitulation event still accelerating downward than a
        reward/risk-favorable entry. Returns (too_extended, extension_pct).
        extension_pct is positive when price is below the EMA (0.0 or
        negative when at/above it, which is never "too extended").
        """
        s = get_settings()
        out = df.dropna()
        if out.empty or "ema_20" not in out.columns:
            return False, 0.0
        last = out.iloc[-1]
        ema20 = float(last["ema_20"])
        close = float(last["close"])
        if ema20 <= 0:
            return False, 0.0
        extension_pct = (ema20 - close) / ema20
        return extension_pct > s.max_dip_extension_pct, extension_pct

    def _pullback_setup(self, df: pd.DataFrame) -> tuple[bool, float, float]:
        """Trend pullback entry for non-oversold regimes.

        Ready when price structure remains above EMA20/EMA50, pullback depth is
        near EMA20 (<=7%), and RSI is not overbought (35..70).
        """
        out = df.dropna()
        if out.empty or any(c not in out.columns for c in ("rsi_14", "ema_20", "ema_50", "close")):
            return False, 0.0, 0.0
        last = out.iloc[-1]
        rsi = float(last["rsi_14"])
        close = float(last["close"])
        ema20 = float(last["ema_20"])
        ema50 = float(last["ema_50"])
        if ema20 <= 0:
            return False, rsi, 0.0
        dist_pct = (close - ema20) / ema20
        ready = bool(
            close > ema20
            and close > ema50
            and dist_pct <= 0.07
            and 35 <= rsi <= 70
        )
        return ready, rsi, dist_pct

    def _mean_reversion_exit(self, df: pd.DataFrame) -> tuple[bool, float]:
        s = get_settings()
        out = df.dropna()
        if out.empty or "rsi_14" not in out.columns:
            return False, 0.0
        rsi = float(out.iloc[-1]["rsi_14"])
        return rsi > s.mean_reversion_exit_rsi, rsi

    def _momentum_deteriorating(self, df: pd.DataFrame) -> bool:
        """MACD histogram declining bar-over-bar — momentum losing steam even
        if RSI/price still look fine. Used as one of the two independent
        confirmations required before the RSI-recovery exit is honored."""
        out = df.dropna()
        if len(out) < 2 or "macd_hist" not in out.columns:
            return False
        return float(out.iloc[-1]["macd_hist"]) < float(out.iloc[-2]["macd_hist"])

    def _bearish_price_confirmation(self, df: pd.DataFrame) -> bool:
        """Price has dropped back below its own EMA20 — the short-term trend
        itself has turned, not just a single indicator wobble."""
        out = df.dropna()
        if out.empty or "ema_20" not in out.columns:
            return False
        last = out.iloc[-1]
        return float(last["close"]) < float(last["ema_20"])

    def _score_entry_candidates(self, df: pd.DataFrame) -> dict[str, EntryCandidate]:
        """Score 4 candidate entry types for observability (NOT for gating).

        2026-08-21 walk-forward (scripts/walkforward.py --market-filter, 25
        symbols, 1d, 3 folds): only `oversold_bounce` was ROBUST + (mean
        +16.5%, positive in every fold with trades) when gated by the BTC
        regime hard-block; `pullback_to_ema`/`ma_reversion` were negative in
        most folds and `breakout_momentum` was mixed — none of the three
        showed a validated edge, so they stay observability-only here.
        """
        out = df.dropna()
        needed = {"rsi_14", "bb_lower", "ema_20", "ema_50", "macd_hist", "volume", "vol_sma_20"}
        if out.empty or len(out) < 51 or not needed <= set(out.columns):
            empty = EntryCandidate(False, 0, {"reason": "insufficient_data"})
            return {k: empty for k in ("oversold_bounce", "pullback_to_ema", "breakout_momentum", "ma_reversion")}

        last = out.iloc[-1]
        close = float(last["close"])
        rsi = float(last["rsi_14"])
        bb_lower = float(last["bb_lower"])
        ema20 = float(last["ema_20"])
        ema50 = float(last["ema_50"])
        macd_hist = float(last["macd_hist"])
        prev_macd_hist = float(out.iloc[-2]["macd_hist"])
        vol = float(last["volume"])
        vol_avg = float(last["vol_sma_20"])
        low5 = float(out["low"].tail(5).min())
        high20_excl_last = float(out["high"].iloc[-21:-1].max()) if len(out) > 20 else float("inf")
        sma50 = float(out["close"].tail(50).mean())
        prev_close = float(out.iloc[-2]["close"])
        prev2_close = float(out.iloc[-3]["close"]) if len(out) > 2 else prev_close

        # 1. OVERSOLD_BOUNCE — deeper/looser dip-buy than the live one, but
        # requires the price already off its 5-day low (not still in free-fall).
        bounce_ready = bool(rsi < 40 and close < bb_lower * 1.02 and low5 > 0 and close > low5 * 1.05)
        bounce_score = _clamp(70 + int(15 * max(0.0, (40 - rsi) / 40)), 70, 85) if bounce_ready else 0

        # 2. PULLBACK_TO_EMA — uptrend pullback to EMA20, RSI cooled to 45-55,
        # MACD histogram turning back up.
        ema_dist_pct = abs(close - ema20) / ema20 if ema20 > 0 else 1.0
        pullback_ready = bool(
            ema_dist_pct <= 0.02 and 45 <= rsi <= 55 and ema20 > ema50 and macd_hist > prev_macd_hist
        )
        pullback_score = _clamp(75 + int(15 * (1 - min(ema_dist_pct / 0.02, 1.0))), 75, 90) if pullback_ready else 0

        # 3. BREAKOUT_MOMENTUM — 20-day high break on >1.5x volume, RSI rising
        # but not yet overbought.
        vol_ratio = (vol / vol_avg) if vol_avg > 0 else 0.0
        breakout_ready = bool(close > high20_excl_last and vol_ratio > 1.5 and 55 < rsi < 75)
        breakout_score = _clamp(80 + int(15 * min((vol_ratio - 1.5) / 1.5, 1.0)), 80, 95) if breakout_ready else 0

        # 4. MA_REVERSION ("VWAP_REVERSION" requested; daily candles have no
        # intraday VWAP, so this is a literal 50-day SMA reversion instead).
        sma_dist_pct = abs(close - sma50) / sma50 if sma50 > 0 else 1.0
        ma_reversion_ready = bool(sma_dist_pct <= 0.01 and rsi < 50 and prev_close > prev2_close)
        ma_reversion_score = _clamp(65 + int(15 * (50 - rsi) / 50), 65, 80) if ma_reversion_ready else 0

        return {
            "oversold_bounce": EntryCandidate(bounce_ready, bounce_score, {"rsi": rsi, "low5": low5}),
            "pullback_to_ema": EntryCandidate(pullback_ready, pullback_score, {"ema_dist_pct": ema_dist_pct, "rsi": rsi}),
            "breakout_momentum": EntryCandidate(breakout_ready, breakout_score, {"vol_ratio": vol_ratio, "rsi": rsi}),
            "ma_reversion": EntryCandidate(ma_reversion_ready, ma_reversion_score, {"sma_dist_pct": sma_dist_pct, "rsi": rsi}),
        }

    def _btc_risk_on(self, btc_1d: pd.DataFrame) -> tuple[bool, float, float]:
        out = btc_1d.dropna()
        if len(out) < 60:
            return False, 0.0, 0.0
        ema50 = float(out.iloc[-1]["ema_50"])
        ema200 = float(out.iloc[-1]["ema_200"])
        if ema200 <= 0:
            return False, ema50, ema200
        return ema50 >= ema200, ema50, ema200

    def _latest_quote_volume(self, df: pd.DataFrame) -> float:
        out = df.dropna()
        if out.empty:
            return 0.0
        if "quote_volume" in out.columns:
            return float(out.iloc[-1]["quote_volume"])
        last = out.iloc[-1]
        return float(last["close"] * last["volume"])

    def _near_news_event(self) -> tuple[bool, str]:
        s = get_settings()
        raw = (s.profitstream_news_events_utc or "").strip()
        if not raw:
            return False, ""
        now = datetime.now(timezone.utc)
        buf = timedelta(minutes=s.profitstream_news_buffer_minutes)
        for token in [p.strip() for p in raw.split(",") if p.strip()]:
            try:
                event_dt = datetime.fromisoformat(token)
                if event_dt.tzinfo is None:
                    event_dt = event_dt.replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if abs(now - event_dt) <= buf:
                return True, token
        return False, ""

    async def _spread_pct(self, symbol: str) -> Optional[float]:
        try:
            raw = await self._client.order_book(symbol, limit=5)
            bids = [(Decimal(str(x[0])), Decimal(str(x[1]))) for x in raw.get("bids", []) if x]
            asks = [(Decimal(str(x[0])), Decimal(str(x[1]))) for x in raw.get("asks", []) if x]
            metrics = analyze_order_book(bids, asks)
            if metrics is None:
                return None
            return float(metrics.spread_pct)
        except Exception:  # noqa: BLE001
            return None

    def _is_held(self, symbol: str, mode: str) -> bool:
        return self._held_position(symbol, mode) is not None

    def _held_position(self, symbol: str, mode: str) -> Optional[dict]:
        return next(
            (p for p in storage.all_positions() if p["symbol"] == symbol and p["mode"] == mode),
            None,
        )
