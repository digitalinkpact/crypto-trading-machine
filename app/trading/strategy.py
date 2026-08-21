"""ProfitStream strategy surface.

The original low-timeframe momentum stack was negative out of sample on this
repository's walk-forward harness. The only candidate with a repeatable edge is
daily dip-buy mean reversion gated by BTC's trend, so this module now follows
that evidence instead of trying to tune the losing micro-timeframe confluence.
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


class ProfitStreamStrategy:
    """Evidence-led dip-buy strategy with explainable rejects."""

    def __init__(self, client: Optional[BinanceUSClient] = None) -> None:
        self._client = client or BinanceUSClient()

    async def analyze_symbol(self, symbol: str, *, mode: str) -> StrategyDecision:
        reasons: list[str] = []
        indicators: dict[str, Any] = {"symbol": symbol}

        try:
            df_1d = await self._candles(symbol, "1d", 320)
            btc_1d = await self._candles("BTCUSDT", "1d", 320)
        except Exception as exc:  # noqa: BLE001
            reasons.append(f"data_unavailable:{exc}")
            return StrategyDecision(symbol, SignalAction.HOLD, 0, reasons, indicators)

        if min(len(df_1d), len(btc_1d)) < 60:
            reasons.append("insufficient_history")
            return StrategyDecision(symbol, SignalAction.HOLD, 0, reasons, indicators)

        held = self._is_held(symbol, mode)
        exit_ready, exit_rsi = self._mean_reversion_exit(df_1d)
        dip_ready, dip_rsi = self._dip_buy_setup(df_1d)
        btc_risk_on, btc_ema50, btc_ema200 = self._btc_risk_on(btc_1d)
        btc_regime = compute_btc_regime_score(btc_1d)
        daily_quote = self._latest_quote_volume(df_1d)
        extended, extension_pct = self._dip_too_extended(df_1d)

        indicators.update(
            {
                "rsi_1d": dip_rsi,
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
            }
        )

        if held and exit_ready:
            indicators["decision"] = "sell_mean_reversion_exit"
            return StrategyDecision(symbol, SignalAction.SELL, 90, reasons, indicators)

        filt_ok = True

        s = get_settings()
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
        if spread_pct is not None and spread_pct > 0.0025:
            filt_ok = False
            reasons.append(f"spread_wide:{spread_pct:.4%}>0.2500%")

        if not btc_risk_on:
            filt_ok = False
            reasons.append("btc_trend_not_aligned")

        score = 0
        score += 70 if dip_ready else 0
        score += 20 if btc_risk_on else 0
        score += 10 if filt_ok else 0

        if not dip_ready:
            if dip_rsi >= 30:
                reasons.append("rsi_not_oversold")
            if float(df_1d.iloc[-1]["close"]) > float(df_1d.iloc[-1]["bb_lower"]):
                reasons.append("close_above_lower_band")

        if held:
            reasons.append("position_already_open")
            return StrategyDecision(symbol, SignalAction.HOLD, score, reasons, indicators)

        if dip_ready and filt_ok:
            indicators["decision"] = "buy"
            return StrategyDecision(symbol, SignalAction.BUY, max(score, 90), reasons, indicators)

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

    def _mean_reversion_exit(self, df: pd.DataFrame) -> tuple[bool, float]:
        out = df.dropna()
        if out.empty or "rsi_14" not in out.columns:
            return False, 0.0
        rsi = float(out.iloc[-1]["rsi_14"])
        return rsi > 55, rsi

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
        return any(p["symbol"] == symbol and p["mode"] == mode for p in storage.all_positions())
