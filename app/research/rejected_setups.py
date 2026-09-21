"""Stage 1 — Rejected-setup outcome tracking (READ-ONLY).

Purpose
-------
Learn, honestly and cheaply, whether the strategy's *rejected* BUY setups would
have made or lost money — without changing any live behaviour. For every BUY
setup that a tick was ready to take but did NOT (blocked by the regime gate,
volume, spread, extension guard, score threshold, etc.), this module simulates
the hypothetical trade from later real daily candles, applying the CURRENT live
exit ladder (ATR stop -> TP1 -> TP2 -> trailing -> stale -> max-hold) with real
fee rates plus a slippage estimate, checking stops/targets against candle
high/low with stop-first priority when both are touched in the same bar.

Safety
------
* Reads the existing ``tick_audit`` table only (via the sanctioned storage read
  path); never writes to any live table.
* Results are written to a SEPARATE research database file (default
  ``<data_cache_dir>/research.db``) — never the live ``trading.db`` tables.
* Market data is fetched read-only from the public Binance.US klines endpoint
  via ``httpx``. Nothing here imports an order-placement path or the live tick
  loop, and it can only ever run when invoked explicitly by a human.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pandas as pd

from app.config import get_settings
from app.logging_setup import get_logger
from app.storage import storage as live_storage
from app.ta import add_indicators

log = get_logger(__name__)

# Slippage estimate applied on both entry and exit fills (fraction). Matches the
# ``ml_label_slippage_pct`` default the ML win/loss labeler already uses, so the
# hypothetical PnL is charged the same trading-cost assumption as live learning.
DEFAULT_SLIPPAGE_PCT = 0.0010

# Below this many samples a grouped result is "insufficient evidence".
MIN_SAMPLES = 30

CandleFetcher = Callable[[str], Optional[pd.DataFrame]]


# ── Rejection classification ────────────────────────────────────────────────

# Raw ``tick_audit.reason`` tokens -> normalized reject-reason categories. A
# single rejected setup can carry several (e.g. spread + score_threshold).
_REASON_TOKENS: tuple[tuple[str, str], ...] = (
    ("low_volume", "volume"),
    ("spread_wide", "spread"),
    ("extension_too_deep", "extension_guard"),
    ("news_blackout", "news_blackout"),
    ("market_gate", "regime_gate"),
    ("orderbook_gate", "orderbook"),
    ("ml_gate", "ml_gate"),
    ("low_confidence", "low_confidence"),
    ("cooldown", "cooldown"),
    ("risk_cap", "position_cap"),
    ("trend_gate", "trend_gate"),
    ("funding_gate", "funding_gate"),
    ("onchain_gate", "onchain_gate"),
    ("insufficient_usdt", "insufficient_usdt"),
)

# Categories that come from a *hard* strategy filter (filt_ok=False). When one
# of these is present the setup was blocked by the filter, not by the score bar.
_HARD_FILTER_CATEGORIES = {"volume", "spread", "extension_guard", "news_blackout"}


@dataclass
class RejectedSetup:
    tick_id: int
    ts: str
    symbol: str
    mode: str
    entry_type: str
    score: int
    btc_regime_label: str
    reject_reasons: list[str]
    raw_reason: str


def _reasons_list(raw: str) -> list[str]:
    return [r.strip() for r in str(raw or "").split(";") if r.strip()]


def classify_rejection(row: dict[str, Any]) -> Optional[RejectedSetup]:
    """Return a ``RejectedSetup`` if ``row`` is a genuine BUY setup that formed
    but was NOT taken; otherwise ``None``.

    A "genuine setup" means the strategy actually identified a dip or pullback
    entry (not merely a no-signal tick). Rows with no setup, executed BUYs, and
    already-held positions are excluded so we only ever simulate real
    counterfactual entries.
    """
    if int(row.get("executed") or 0) == 1:
        return None

    raw_reason = str(row.get("reason") or "")
    if "insufficient_history" in raw_reason:
        return None
    if "position_already_open" in raw_reason or "already_held" in raw_reason:
        return None

    try:
        ind = json.loads(row.get("indicators") or "{}")
    except (TypeError, ValueError):
        ind = {}
    if not isinstance(ind, dict):
        ind = {}

    # Was an entry setup actually ready? Three independent signals of readiness.
    pullback_ready = bool(ind.get("pullback_ready"))
    decision_buy = str(ind.get("decision") or "") == "buy"
    has_dip_eval = "rsi_1d" in ind and "bb_lower_1d" in ind
    dip_ready = (
        has_dip_eval
        and "rsi_not_oversold" not in raw_reason
        and "close_above_lower_band" not in raw_reason
    )
    if not (pullback_ready or decision_buy or dip_ready):
        return None

    # Normalize the reject reasons that actually blocked this ready setup.
    norm: set[str] = set()
    for token, cat in _REASON_TOKENS:
        if token in raw_reason:
            norm.add(cat)
    # Hard BTC regime block (not the soft scoring penalty "..._soft").
    if re.search(r"btc_trend_not_aligned(?!_soft)", raw_reason):
        norm.add("regime_gate")

    # A ready setup that cleared every hard filter but still wasn't executed was
    # blocked by the score threshold (score < profitstream_score_threshold).
    try:
        filters = json.loads(row.get("filters") or "{}")
    except (TypeError, ValueError):
        filters = {}
    threshold = int(filters.get("score_threshold") or 0) if isinstance(filters, dict) else 0
    score = int(row.get("score") or 0)
    if not (norm & _HARD_FILTER_CATEGORIES):
        if threshold and score < threshold:
            norm.add("score_threshold")
    if not norm:
        # Ready + not executed + nothing else identified -> score bar by default.
        norm.add("score_threshold")

    entry_type = str(ind.get("entry_strategy") or "")
    if not entry_type:
        entry_type = "pullback" if pullback_ready else "dip_buy"

    return RejectedSetup(
        tick_id=int(row.get("id") or 0),
        ts=str(row.get("ts") or ""),
        symbol=str(row.get("symbol") or ""),
        mode=str(row.get("mode") or ""),
        entry_type=entry_type,
        score=score,
        btc_regime_label=str(ind.get("btc_regime_label") or ""),
        reject_reasons=sorted(norm),
        raw_reason=raw_reason,
    )


# ── Exit-ladder simulation ──────────────────────────────────────────────────

@dataclass
class LadderParams:
    stop_loss_pct: float
    atr_stop_enabled: bool
    atr_stop_multiple: float
    atr_stop_min_pct: float
    atr_stop_max_pct: float
    tp1_pct: float
    tp1_frac: float
    tp2_pct: float
    tp2_frac: float
    trail_activation: float
    trail_distance: float
    trailing_requires_tp1: bool
    stale_enabled: bool
    stale_hours: float
    stale_max_pnl: float
    max_hold_hours: float
    fee_rate: float
    slippage_pct: float


def ladder_params_from_settings(slippage_pct: float = DEFAULT_SLIPPAGE_PCT) -> LadderParams:
    """Snapshot the CURRENT live exit-ladder configuration (read-only)."""
    s = get_settings()
    return LadderParams(
        stop_loss_pct=float(s.stop_loss_pct),
        atr_stop_enabled=bool(getattr(s, "atr_stop_enabled", False)),
        atr_stop_multiple=float(getattr(s, "atr_stop_multiple", 2.0)),
        atr_stop_min_pct=float(getattr(s, "atr_stop_min_pct", 0.02)),
        atr_stop_max_pct=float(getattr(s, "atr_stop_max_pct", 0.08)),
        tp1_pct=float(getattr(s, "take_profit_1_pct", 0.08)),
        tp1_frac=float(getattr(s, "take_profit_1_fraction", 0.50)),
        tp2_pct=float(getattr(s, "take_profit_2_pct", 0.15)),
        tp2_frac=float(getattr(s, "take_profit_2_fraction", 0.25)),
        trail_activation=float(getattr(s, "trailing_activation_pct", 0.05)),
        trail_distance=float(s.trailing_stop_pct),
        trailing_requires_tp1=bool(getattr(s, "trailing_requires_tp1", False)),
        stale_enabled=bool(getattr(s, "stale_exit_enabled", True)),
        stale_hours=float(getattr(s, "stale_exit_hours", 48)),
        stale_max_pnl=float(getattr(s, "stale_exit_max_pnl_pct", 0.02)),
        max_hold_hours=float(getattr(s, "max_hold_hours", 96)),
        fee_rate=float(s.binance_taker_fee),
        slippage_pct=float(slippage_pct),
    )


def _stop_fraction(p: LadderParams, atr_pct: Optional[float]) -> float:
    """Replicate ``risk.dynamic_stop_pct`` exactly (ATR-scaled, clamped)."""
    if not p.atr_stop_enabled or not atr_pct or atr_pct <= 0:
        return p.stop_loss_pct
    raw = p.atr_stop_multiple * atr_pct
    return max(p.atr_stop_min_pct, min(p.atr_stop_max_pct, raw))


@dataclass
class SimResult:
    entry_price: float
    exit_price: float
    pnl_pct: float
    exit_reason: str
    mfe_pct: float
    mae_pct: float
    hold_hours: float
    n_legs: int
    unresolved: bool


def simulate_exit(
    candles: pd.DataFrame,
    signal_ts: datetime,
    atr_pct: Optional[float],
    p: LadderParams,
    *,
    min_forward_bars: int = 2,
) -> Optional[SimResult]:
    """Simulate a hypothetical long from the next executable price after
    ``signal_ts``, applying the live exit ladder against real candle high/low.

    Entry is the OPEN of the first daily candle to open strictly after the
    signal timestamp (the next executable price), adjusted up for slippage.
    Within each bar, a stop touch is resolved before any target (stop-first).
    Partial TP1/TP2 legs scale out; the remainder rides the trailing/stale/
    max-hold rules. Returns ``None`` when there isn't enough forward data.
    """
    if "open_time" not in candles.columns:
        return None
    future = candles[candles["open_time"] > signal_ts]
    if len(future) < min_forward_bars:
        return None

    raw_entry = float(future.iloc[0]["open"])
    if raw_entry <= 0:
        return None
    entry_eff = raw_entry * (1.0 + p.slippage_pct)

    stop_frac = _stop_fraction(p, atr_pct)
    stop_price = raw_entry * (1.0 - stop_frac)
    tp1_price = raw_entry * (1.0 + p.tp1_pct)
    tp2_price = raw_entry * (1.0 + p.tp2_pct)

    total_qty = 1.0
    qty = 1.0
    tp1_taken = False
    tp2_taken = False
    hwm = raw_entry
    lwm = raw_entry
    realized_pnl = 0.0
    legs = 0
    last_exit_price = raw_entry
    exit_reason: Optional[str] = None
    entry_ot = future.iloc[0]["open_time"]
    exit_ot = entry_ot

    def _sell(level: float, q: float) -> None:
        nonlocal realized_pnl, legs, last_exit_price
        if q <= 0:
            return
        exit_eff = level * (1.0 - p.slippage_pct)
        gross = q * (exit_eff - entry_eff)
        fees = q * entry_eff * p.fee_rate + q * exit_eff * p.fee_rate
        realized_pnl += gross - fees
        legs += 1
        last_exit_price = level

    for i in range(len(future)):
        bar = future.iloc[i]
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])
        exit_ot = bar["open_time"]
        elapsed_h = (exit_ot - entry_ot).total_seconds() / 3600.0
        hwm_prior = hwm  # trailing floor uses prior bars only (no intrabar look-ahead)

        # 1. Stop-loss FIRST (if the bar's low pierced the stop).
        if low <= stop_price:
            _sell(stop_price, qty)
            exit_reason = "stop_loss"
            qty = 0.0
            break

        # 2. Take-profit scale-outs on the up-move.
        if not tp1_taken and high >= tp1_price:
            leg = min(qty, total_qty * p.tp1_frac)
            _sell(tp1_price, leg)
            qty -= leg
            tp1_taken = True
            exit_reason = "take_profit_1"
        if tp1_taken and not tp2_taken and qty > 1e-9 and high >= tp2_price:
            leg = min(qty, total_qty * p.tp2_frac)
            _sell(tp2_price, leg)
            qty -= leg
            tp2_taken = True
            exit_reason = "take_profit_2"

        # 3. Trailing stop on the remainder (armed from prior-bar HWM).
        armed = (not p.trailing_requires_tp1 or tp1_taken) and hwm_prior > raw_entry * (
            1.0 + p.trail_activation
        )
        if armed and qty > 1e-9:
            trail_floor = hwm_prior * (1.0 - p.trail_distance)
            if low <= trail_floor:
                _sell(trail_floor, qty)
                exit_reason = "trailing_stop"
                qty = 0.0
                break

        # Update excursions AFTER the trailing check.
        hwm = max(hwm, high)
        lwm = min(lwm, low)

        # 4. Stale "dead money" exit, then 5. max-hold — evaluated at bar close.
        if p.stale_enabled and elapsed_h > p.stale_hours and qty > 1e-9:
            change = (close - raw_entry) / raw_entry
            if change < p.stale_max_pnl:
                _sell(close, qty)
                exit_reason = "stale_dead_money"
                qty = 0.0
                break
        if elapsed_h > p.max_hold_hours and qty > 1e-9:
            _sell(close, qty)
            exit_reason = "max_hold"
            qty = 0.0
            break

    unresolved = False
    if qty > 1e-9:
        # Right-censored: never hit a terminal exit within available candles.
        _sell(float(future.iloc[-1]["close"]), qty)
        exit_ot = future.iloc[-1]["open_time"]
        unresolved = True
        if exit_reason is None:
            exit_reason = "unresolved"

    pnl_pct = realized_pnl / (total_qty * entry_eff) if entry_eff > 0 else 0.0
    return SimResult(
        entry_price=raw_entry,
        exit_price=last_exit_price,
        pnl_pct=pnl_pct,
        exit_reason=exit_reason or "unresolved",
        mfe_pct=(hwm - raw_entry) / raw_entry,
        mae_pct=(raw_entry - lwm) / raw_entry,
        hold_hours=(exit_ot - entry_ot).total_seconds() / 3600.0,
        n_legs=legs,
        unresolved=unresolved,
    )


# ── Candle fetch (read-only public market data) ─────────────────────────────

def fetch_daily_candles(
    symbol: str,
    *,
    base_url: Optional[str] = None,
    limit: int = 1000,
    timeout: float = 15.0,
) -> Optional[pd.DataFrame]:
    """Read-only daily OHLCV from the PUBLIC Binance.US klines endpoint.

    Public market data — no API key, no auth, no order path. Returns a
    DataFrame indexed by ``close_time`` with an ``open_time`` column plus
    ATR/indicator columns from ``add_indicators``, or ``None`` on any failure.
    """
    base = (base_url or get_settings().binance_base_url).rstrip("/")
    url = f"{base}/api/v3/klines"
    try:
        resp = httpx.get(
            url, params={"symbol": symbol, "interval": "1d", "limit": limit}, timeout=timeout
        )
        resp.raise_for_status()
        raw = resp.json()
    except Exception as exc:  # noqa: BLE001
        log.warning("rejected-setup: klines fetch failed for %s: %s", symbol, exc)
        return None
    if not isinstance(raw, list) or not raw:
        return None

    cols = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_base_volume", "taker_quote_volume", "ignore",
    ]
    df = pd.DataFrame(raw, columns=cols)
    for c in ("open", "high", "low", "close", "volume", "quote_volume"):
        df[c] = pd.to_numeric(df[c])
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms", utc=True)
    df = df.set_index("close_time")[
        ["open", "high", "low", "close", "volume", "quote_volume", "trades", "open_time"]
    ]
    try:
        df = add_indicators(df)
    except Exception as exc:  # noqa: BLE001
        log.debug("rejected-setup: add_indicators failed for %s: %s", symbol, exc)
    return df


def atr_pct_at(candles: pd.DataFrame, signal_ts: datetime) -> Optional[float]:
    """ATR% (atr_14/close) at the last candle that had closed by ``signal_ts``.

    Matches the live ``autopilot._atr_pct`` definition so the simulated stop
    uses the same volatility the bot would have seen at signal time.
    """
    if "atr_14" not in candles.columns or "open_time" not in candles.columns:
        return None
    prior = candles[candles["open_time"] <= signal_ts]
    if prior.empty:
        return None
    last = prior.iloc[-1]
    try:
        close = float(last["close"])
        atr = float(last["atr_14"])
    except (TypeError, ValueError):
        return None
    if close <= 0 or atr <= 0 or pd.isna(atr):
        return None
    return atr / close


# ── Research storage (SEPARATE database — never the live tables) ────────────

_RESEARCH_SCHEMA = """
CREATE TABLE IF NOT EXISTS rejected_setup_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tick_id INTEGER UNIQUE,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    mode TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    score INTEGER NOT NULL,
    btc_regime_label TEXT,
    reject_reasons TEXT NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    sim_pnl_pct REAL NOT NULL,
    exit_reason TEXT NOT NULL,
    mfe_pct REAL,
    mae_pct REAL,
    hold_hours REAL,
    n_legs INTEGER,
    unresolved INTEGER NOT NULL DEFAULT 0,
    computed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_rso_symbol ON rejected_setup_outcomes(symbol);
"""


def default_research_db() -> Path:
    return Path(get_settings().data_cache_dir) / "research.db"


class ResearchStore:
    """Tiny SQLite wrapper for the SEPARATE research database.

    This intentionally does NOT reuse the live ``Storage`` class or the live
    ``trading.db`` file — the research outputs must be physically isolated from
    everything the trading bot reads or writes.
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else default_research_db()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(_RESEARCH_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def existing_tick_ids(self) -> set[int]:
        with self._conn() as c:
            rows = c.execute("SELECT tick_id FROM rejected_setup_outcomes").fetchall()
        return {int(r["tick_id"]) for r in rows}

    def insert(self, rs: RejectedSetup, sim: SimResult) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO rejected_setup_outcomes("
                "tick_id,ts,symbol,mode,entry_type,score,btc_regime_label,reject_reasons,"
                "entry_price,exit_price,sim_pnl_pct,exit_reason,mfe_pct,mae_pct,hold_hours,"
                "n_legs,unresolved,computed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rs.tick_id, rs.ts, rs.symbol, rs.mode, rs.entry_type, rs.score,
                    rs.btc_regime_label, json.dumps(rs.reject_reasons),
                    sim.entry_price, sim.exit_price, sim.pnl_pct, sim.exit_reason,
                    sim.mfe_pct, sim.mae_pct, sim.hold_hours, sim.n_legs,
                    1 if sim.unresolved else 0,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def all_outcomes(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM rejected_setup_outcomes").fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            try:
                d["reject_reasons"] = json.loads(d.get("reject_reasons") or "[]")
            except (TypeError, ValueError):
                d["reject_reasons"] = []
            out.append(d)
        return out


# ── Orchestration ───────────────────────────────────────────────────────────

@dataclass
class BuildSummary:
    tick_rows_scanned: int = 0
    rejected_setups: int = 0
    simulated: int = 0
    stored: int = 0
    skipped_existing: int = 0
    skipped_no_data: int = 0
    symbols: set[str] = field(default_factory=set)


def _parse_ts(ts: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def build_rejected_setup_outcomes(
    *,
    mode: str = "live",
    db_path: Optional[Path] = None,
    source_storage=live_storage,
    fetch_candles: Optional[CandleFetcher] = None,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
    scan_limit: int = 500_000,
    max_symbols: Optional[int] = None,
) -> BuildSummary:
    """Scan ``tick_audit`` (read-only), simulate every rejected BUY setup, and
    store outcomes in the SEPARATE research database.

    ``fetch_candles`` is injectable for testing; by default it fetches read-only
    daily candles from the public Binance.US klines endpoint. Candles are cached
    per symbol for the duration of the run. Already-simulated ticks are skipped
    (idempotent).
    """
    p = ladder_params_from_settings(slippage_pct)
    store = ResearchStore(db_path)
    fetch = fetch_candles or fetch_daily_candles
    existing = store.existing_tick_ids()
    candle_cache: dict[str, Optional[pd.DataFrame]] = {}
    summary = BuildSummary()

    rows = source_storage.recent_tick_audit(limit=scan_limit)
    summary.tick_rows_scanned = len(rows)

    for row in rows:
        if str(row.get("mode") or "") != mode:
            continue
        rs = classify_rejection(row)
        if rs is None:
            continue
        summary.rejected_setups += 1
        if rs.tick_id in existing:
            summary.skipped_existing += 1
            continue

        if max_symbols is not None and rs.symbol not in candle_cache and len(summary.symbols) >= max_symbols:
            continue

        signal_ts = _parse_ts(rs.ts)
        if signal_ts is None:
            summary.skipped_no_data += 1
            continue

        if rs.symbol not in candle_cache:
            candle_cache[rs.symbol] = fetch(rs.symbol)
        candles = candle_cache[rs.symbol]
        if candles is None or candles.empty:
            summary.skipped_no_data += 1
            continue

        atr_pct = atr_pct_at(candles, signal_ts)
        sim = simulate_exit(candles, signal_ts, atr_pct, p)
        if sim is None:
            summary.skipped_no_data += 1
            continue

        store.insert(rs, sim)
        summary.simulated += 1
        summary.stored += 1
        summary.symbols.add(rs.symbol)

    return summary


def summarize_by_reason(outcomes: list[dict[str, Any]], *, min_samples: int = MIN_SAMPLES) -> list[dict[str, Any]]:
    """Aggregate stored rejected-setup outcomes by normalized reject reason.

    Each setup counts toward every reason that blocked it. Returns a list of
    per-reason dicts (reason, n, win_rate, expectancy_pct, ci, winners, losers,
    sufficient) sorted by sample size. Pure over the passed rows; used by both
    the standalone report and the weekly one-pager.
    """
    from app.research.bootstrap import bootstrap_ci, expectancy, win_rate

    groups: dict[str, list[float]] = {}
    for r in outcomes:
        pnl = float(r.get("sim_pnl_pct") or 0.0)
        for reason in (r.get("reject_reasons") or ["unknown"]):
            groups.setdefault(reason, []).append(pnl)

    out: list[dict[str, Any]] = []
    for reason, pnls in groups.items():
        lo, hi = bootstrap_ci(pnls)
        out.append({
            "reason": reason,
            "n": len(pnls),
            "win_rate": win_rate(pnls),
            "expectancy_pct": expectancy(pnls),
            "ci": (lo, hi),
            "winners": sum(1 for x in pnls if x > 0),
            "losers": sum(1 for x in pnls if x <= 0),
            "sufficient": len(pnls) >= min_samples,
        })
    out.sort(key=lambda d: -d["n"])
    return out
