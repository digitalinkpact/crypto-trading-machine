"""Risk gates — evaluated on every tick BEFORE looking at agent signals.

Six hard rules enforced here:
  1. Hard stop-loss     (loss > stop_loss_pct           → force SELL)
  2. Take-profit 1      (gain > take_profit_1_pct       → sell 50% of stake)
  3. Take-profit 2      (gain > take_profit_2_pct       → sell 25% of original stake)
  4. Trailing stop      (price drops trailing_stop_pct from HWM after activation)
  5. Max hold time      (entry_ts > max_hold_hours ago  → force SELL)
  6. Drawdown breaker   (paper/live PnL < -drawdown_pct → halt new BUYs)

The final ~25% of a winning position is deliberately never force-closed by a
fixed profit target — once TP1+TP2 have scaled out 75% of the original stake,
the remainder rides the trailing stop so a strong winner can keep running
instead of being capped ("let winners run", not "more trades").

Plus:
  - Volatility-scaled position sizing (size ∝ baseline / atr_pct, clamped)
  - Max open positions cap
  - Max long exposure cap (don't put >X% of equity in non-USDT)

Stored state:
  - kv:hwm:{symbol}  → high-water-mark price seen for the open position
  - kv:tp1:{symbol}  → whether the +8%/50% scale-out has already fired
  - kv:tp2:{symbol}  → whether the +15%/25% scale-out has already fired
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from app.config import get_settings
from app.logging_setup import get_logger
from app.storage import storage

log = get_logger(__name__)


def _hwm_key(symbol: str) -> str:
    return f"hwm:{symbol}"


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


def mark_tp1_taken(symbol: str) -> None:
    storage.kv_set(_tp1_key(symbol), True)


def clear_tp1(symbol: str) -> None:
    storage.kv_set(_tp1_key(symbol), None)


def tp1_taken(symbol: str) -> bool:
    return bool(storage.kv_get(_tp1_key(symbol)) or False)


def mark_tp2_taken(symbol: str) -> None:
    storage.kv_set(_tp2_key(symbol), True)


def clear_tp2(symbol: str) -> None:
    storage.kv_set(_tp2_key(symbol), None)


def tp2_taken(symbol: str) -> bool:
    return bool(storage.kv_get(_tp2_key(symbol)) or False)


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
    reason: str  # "stop_loss" | "take_profit_1" | "take_profit_2" | "trailing_stop" | "max_hold"


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

        # Track HWM for this position.
        hwm = update_hwm(symbol, price)

        change = (price - entry) / entry  # positive = gain, negative = loss

        # 1. Hard stop-loss
        if change <= Decimal(str(-s.stop_loss_pct)):
            out.append(ExitDecision(symbol, qty, "stop_loss"))
            continue

        tp1_fraction = Decimal(str(getattr(s, "take_profit_1_fraction", 0.50)))
        tp2_fraction = Decimal(str(getattr(s, "take_profit_2_fraction", 0.25)))
        already_tp1 = tp1_taken(symbol)
        # Reconstruct the ORIGINAL stake so TP2's fraction is "25% of what we
        # first bought", not "25% of whatever happens to be left after TP1" —
        # a fixed scale-out ladder (sell 50% @ +8%, sell 25% @ +15%, trail the
        # final ~25%), not a shrinking-base percentage.
        original_qty = (qty / (Decimal("1") - tp1_fraction)) if already_tp1 and tp1_fraction < 1 else qty

        # 2. First take-profit scale-out (sell 50% of the stake at +8%)
        tp1 = Decimal(str(getattr(s, "take_profit_1_pct", 0.08)))
        if change >= tp1 and not already_tp1:
            part_qty = qty * tp1_fraction
            if part_qty > 0:
                out.append(ExitDecision(symbol, part_qty, "take_profit_1"))
                mark_tp1_taken(symbol)
                continue

        # 3. Second take-profit scale-out (sell another 25% of the ORIGINAL
        #    stake at +15%). The final slice is deliberately never force-
        #    closed by a fixed target — it rides the trailing stop below so a
        #    strong winner can keep running instead of being capped
        #    ("let winners run", not "more trades").
        tp2 = Decimal(str(getattr(s, "take_profit_2_pct", 0.15)))
        if change >= tp2 and not tp2_taken(symbol):
            part_qty = min(qty, original_qty * tp2_fraction)
            if part_qty > 0:
                out.append(ExitDecision(symbol, part_qty, "take_profit_2"))
                mark_tp2_taken(symbol)
                continue

        # 4. Trailing stop (arm only after position gains the configured threshold)
        trail_activation = Decimal(str(getattr(s, "trailing_activation_pct", s.take_profit_pct / 2)))
        if hwm > entry * (Decimal("1") + trail_activation):
            trail_floor = hwm * (Decimal("1") - Decimal(str(s.trailing_stop_pct)))
            if price <= trail_floor:
                out.append(ExitDecision(symbol, qty, "trailing_stop"))
                continue

        # 5. Max hold time
        try:
            entry_ts = datetime.fromisoformat(pos["entry_ts"])
            if entry_ts.tzinfo is None:
                entry_ts = entry_ts.replace(tzinfo=timezone.utc)
            if now - entry_ts > timedelta(hours=s.max_hold_hours):
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
