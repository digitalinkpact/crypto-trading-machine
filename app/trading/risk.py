"""Risk gates — evaluated on every tick BEFORE looking at agent signals.

Five hard rules enforced here:
  1. Hard stop-loss   (loss > stop_loss_pct          → force SELL)
  2. Take-profit      (gain > take_profit_pct        → force SELL)
  3. Trailing stop    (price drops trailing_stop_pct from HWM after take_profit/2 hit)
  4. Max hold time    (entry_ts > max_hold_hours ago → force SELL)
  5. Drawdown breaker (paper/live PnL < -drawdown_pct → halt new BUYs)

Plus:
  - Volatility-scaled position sizing (size ∝ baseline / atr_pct, clamped)
  - Max open positions cap
  - Max long exposure cap (don't put >X% of equity in non-USDT)

Stored state:
  - kv:hwm:{symbol}  → high-water-mark price seen for the open position
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from app.config import get_settings
from app.logging_setup import get_logger
from app.storage import storage

log = get_logger(__name__)


def _hwm_key(symbol: str) -> str:
    return f"hwm:{symbol}"


def _lwm_key(symbol: str) -> str:
    return f"lwm:{symbol}"


def _tp1_key(symbol: str) -> str:
    return f"tp1:{symbol}"


def _tp2_key(symbol: str) -> str:
    return f"tp2:{symbol}"


def update_hwm(symbol: str, price: Decimal) -> Decimal:
    """Track per-position high-water mark for trailing-stop."""
    cur = storage.kv_get(_hwm_key(symbol))
    cur_d = Decimal(str(cur)) if cur is not None else Decimal("0")
    new = max(cur_d, price)
    if new != cur_d:
        storage.kv_set(_hwm_key(symbol), str(new))
    return new


def clear_hwm(symbol: str) -> None:
    storage.kv_set(_hwm_key(symbol), None)


def update_lwm(symbol: str, price: Decimal) -> Decimal:
    """Track per-position low-water mark — the counterpart to `update_hwm`,
    used to compute MAE (maximum adverse excursion) at close time."""
    cur = storage.kv_get(_lwm_key(symbol))
    if cur is None:
        storage.kv_set(_lwm_key(symbol), str(price))
        return price
    cur_d = Decimal(str(cur))
    new = min(cur_d, price)
    if new != cur_d:
        storage.kv_set(_lwm_key(symbol), str(new))
    return new


def clear_lwm(symbol: str) -> None:
    storage.kv_set(_lwm_key(symbol), None)


def get_lwm(symbol: str) -> Optional[Decimal]:
    cur = storage.kv_get(_lwm_key(symbol))
    if cur in (None, "None"):
        return None
    try:
        return Decimal(str(cur))
    except Exception as e:  # noqa: BLE001
        log.exception("Trade execution failure: %s", e)
        return None


def mfe_mae_pct(symbol: str, entry_price: Decimal) -> tuple[Optional[float], Optional[float]]:
    """Compute (MFE%, MAE%) for an open position from its tracked HWM/LWM,
    relative to entry price. Returns (None, None) if nothing was tracked yet
    (e.g. a position closed before a single risk-loop tick ran)."""
    if entry_price <= 0:
        return None, None
    hwm = get_hwm(symbol)
    lwm = get_lwm(symbol)
    mfe = float((hwm - entry_price) / entry_price) if hwm is not None else None
    mae = float((entry_price - lwm) / entry_price) if lwm is not None else None
    return mfe, mae


def mark_tp1_taken(symbol: str) -> None:
    storage.kv_set(_tp1_key(symbol), True)


def clear_tp1(symbol: str) -> None:
    storage.kv_set(_tp1_key(symbol), None)


def tp1_taken(symbol: str) -> bool:
    return bool(storage.kv_get(_tp1_key(symbol)))


def mark_tp2_taken(symbol: str) -> None:
    storage.kv_set(_tp2_key(symbol), True)


def clear_tp2(symbol: str) -> None:
    storage.kv_set(_tp2_key(symbol), None)


def tp2_taken(symbol: str) -> bool:
    return bool(storage.kv_get(_tp2_key(symbol)))


def infer_exit_reason(agents: Optional[list[str]]) -> str:
    """Derive a closed_trades `exit_reason` from an order's agent tags.

    Risk-gate exits tag the order with `risk:<reason>` (see autopilot's
    `_run_risk_gates`) and take priority — they're the most authoritative
    since they come from the hard stop/TP/trailing/max-hold rules. Next,
    strategy-declared signal exits tag `exit:<reason>` (e.g.
    "mean_reversion_rsi_price" — see ProfitStreamStrategy) so a generic
    "signal" bucket doesn't swallow a known, specific reason. Anything else
    falls back to the generic "signal" bucket.
    """
    for a in agents or []:
        if isinstance(a, str) and a.startswith("risk:"):
            return a.split(":", 1)[1] or "unknown"
    for a in agents or []:
        if isinstance(a, str) and a.startswith("exit:"):
            return a.split(":", 1)[1] or "signal"
    return "signal"



def get_hwm(symbol: str) -> Optional[Decimal]:
    cur = storage.kv_get(_hwm_key(symbol))
    if cur in (None, "None"):
        return None
    try:
        return Decimal(str(cur))
    except Exception as e:  # noqa: BLE001
        log.exception("Trade execution failure: %s", e)
        return None


@dataclass
class ExitDecision:
    symbol: str
    qty: Decimal
    reason: str  # "stop_loss" | "take_profit" | "trailing_stop" | "max_hold"


def evaluate_exits(
    *,
    positions: list[dict],
    prices: dict[str, Decimal],
    now: Optional[datetime] = None,
) -> list[ExitDecision]:
    """Inspect every open position; return ones that hit a hard exit rule."""
    s = get_settings()
    now = now or datetime.now(timezone.utc)
    out: list[ExitDecision] = []

    for pos in positions:
        symbol = pos["symbol"]
        if symbol not in prices:
            continue
        price = prices[symbol]
        entry = Decimal(str(pos["entry_price"]))
        qty = Decimal(str(pos["qty"]))
        if entry <= 0 or qty <= 0:
            continue

        # Track HWM/LWM for this position (drives trailing-stop plus the
        # MFE/MAE forensic metrics recorded at close time).
        hwm = update_hwm(symbol, price)
        update_lwm(symbol, price)

        change = (price - entry) / entry  # positive = gain, negative = loss

        # 1. Hard stop-loss
        if change <= Decimal(str(-s.stop_loss_pct)):
            out.append(ExitDecision(symbol, qty, "stop_loss"))
            continue

        # 2. Take-profit
        tp1_pct = Decimal(str(getattr(s, "take_profit_1_pct", 0.08)))
        tp1_fraction = Decimal(str(getattr(s, "take_profit_1_fraction", 0.50)))
        tp2_pct = Decimal(str(getattr(s, "take_profit_2_pct", 0.15)))
        tp2_fraction = Decimal(str(getattr(s, "take_profit_2_fraction", 0.25)))
        if (not tp1_taken(symbol)) and change >= tp1_pct:
            out.append(ExitDecision(symbol, qty * tp1_fraction, "take_profit_1"))
            continue
        if tp1_taken(symbol) and (not tp2_taken(symbol)) and change >= tp2_pct:
            original_qty = qty
            remainder_fraction = Decimal("1") - tp1_fraction
            if remainder_fraction > 0:
                original_qty = qty / remainder_fraction
            out.append(ExitDecision(symbol, min(qty, original_qty * tp2_fraction), "take_profit_2"))
            continue

        # 3. Trailing stop (arm only after position gains the configured threshold)
        trail_activation = Decimal(str(getattr(s, "trailing_activation_pct", s.take_profit_pct / 2)))
        if hwm > entry * (Decimal("1") + trail_activation):
            trail_floor = hwm * (Decimal("1") - Decimal(str(s.trailing_stop_pct)))
            if price <= trail_floor:
                out.append(ExitDecision(symbol, qty, "trailing_stop"))
                continue

        # 4. Stale / "dead money" exit — held a while without meaningfully
        # moving in our favor. Only ever forces an EXIT (frees the slot for a
        # fresher signal); never loosens a stop or widens a target.
        try:
            entry_ts = datetime.fromisoformat(pos["entry_ts"])
            if entry_ts.tzinfo is None:
                entry_ts = entry_ts.replace(tzinfo=timezone.utc)
            hours_held = (now - entry_ts).total_seconds() / 3600.0
            if getattr(s, "stale_exit_enabled", True):
                stale_hours = getattr(s, "stale_exit_hours", 48)
                stale_max_pnl = Decimal(str(getattr(s, "stale_exit_max_pnl_pct", 0.02)))
                if hours_held > stale_hours and change < stale_max_pnl:
                    out.append(ExitDecision(symbol, qty, "stale_dead_money"))
                    continue

            # 5. Max hold time
            if hours_held > s.max_hold_hours:
                out.append(ExitDecision(symbol, qty, "max_hold"))
        except Exception as e:  # noqa: BLE001
            log.exception("Trade execution failure: %s", e)
            continue

    return out


# ─── Drawdown circuit breaker ──────────────────────────────────────────────


def is_circuit_breaker_tripped(
    *,
    starting_balance: Optional[Decimal],
    current_balance: Decimal,
) -> tuple[bool, float]:
    """Return (tripped, drawdown_pct).

    Drawdown is measured against the autopilot's starting balance for this run.
    If we don't know the starting balance, the breaker can't trip.
    """
    if not starting_balance or starting_balance <= 0:
        return False, 0.0
    dd = (current_balance - starting_balance) / starting_balance
    threshold = Decimal(str(get_settings().drawdown_circuit_breaker_pct))
    return (dd <= -threshold), float(dd)


# ─── Daily loss limit ───────────────────────────────────────────────────────


def is_daily_loss_limit_tripped(
    *,
    mode: str,
    starting_balance: Optional[Decimal],
    now: Optional[datetime] = None,
) -> tuple[bool, Decimal]:
    """Return (tripped, today_realized_pnl).

    Distinct from the cumulative drawdown breaker: sums realized PnL from
    `closed_trades` for the current UTC calendar day and halts new BUYs once
    the loss exceeds `daily_loss_limit_pct` of starting equity. Resets at UTC
    midnight simply because trades from a prior day no longer match `today`.
    FAIL-OPEN: disabled, or no starting balance known, never trips.
    """
    s = get_settings()
    if not getattr(s, "daily_loss_limit_enabled", True):
        return False, Decimal("0")
    if not starting_balance or starting_balance <= 0:
        return False, Decimal("0")

    now = now or datetime.now(timezone.utc)
    today = now.date()
    today_pnl = Decimal("0")
    for t in storage.closed_trades(limit=500):
        if t.get("mode") != mode:
            continue
        exit_ts_raw = t.get("exit_ts")
        if not exit_ts_raw:
            continue
        try:
            exit_ts = datetime.fromisoformat(str(exit_ts_raw))
            if exit_ts.tzinfo is None:
                exit_ts = exit_ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if exit_ts.date() != today:
            continue
        today_pnl += Decimal(str(t.get("pnl", 0)))

    limit = starting_balance * Decimal(str(s.daily_loss_limit_pct))
    return (today_pnl <= -limit), today_pnl



# ─── Position sizing helpers ───────────────────────────────────────────────


def volatility_scaled_pct(
    base_pct: float,
    atr_pct: Optional[float],
    *,
    target_atr_pct: float = 0.020,  # ~2% daily move = "average" crypto volatility
    floor: float = 0.5,
    ceiling: float = 1.5,
) -> float:
    """Scale position size so that quieter coins get bigger size, wilder coins smaller.

    multiplier = clamp(target_atr_pct / atr_pct, floor, ceiling)
    """
    if not atr_pct or atr_pct <= 0:
        return base_pct
    raw = target_atr_pct / atr_pct
    mult = max(floor, min(ceiling, raw))
    return base_pct * mult


def can_open_new_position(
    *,
    open_positions: int,
    long_exposure_pct: float,
) -> tuple[bool, str]:
    """Cap concurrent positions and total non-USDT exposure."""
    s = get_settings()
    if open_positions >= s.max_open_positions:
        return False, f"max_open_positions={s.max_open_positions} reached"
    if long_exposure_pct >= s.max_long_exposure_pct:
        return False, f"long_exposure {long_exposure_pct:.0%} >= cap {s.max_long_exposure_pct:.0%}"
    return True, ""
