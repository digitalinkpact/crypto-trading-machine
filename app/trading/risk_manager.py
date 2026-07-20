"""ProfitStream risk manager.

Implements trade-quality and capital-preservation rules on top of exchange
filters and autopilot safety gates.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.config import get_settings
from app.storage import storage


@dataclass
class EntryRiskDecision:
    allow: bool
    reason: str
    notional_usdt: Decimal


class RiskManager:
    """Position/risk policy for new entries."""

    def evaluate_entry(
        self,
        *,
        mode: str,
        total_equity_usdt: Decimal,
        open_positions: int,
        long_exposure_pct: float,
        entry_price: Decimal,
        aggressive_mode: bool,
        is_pyramid: bool = False,
        current_position_notional: Decimal | None = None,
    ) -> EntryRiskDecision:
        s = get_settings()

        max_open_positions = (
            getattr(s, "aggressive_max_open_positions", 10)
            if aggressive_mode else getattr(s, "rollback_max_open_positions", getattr(s, "max_open_positions", 3))
        )
        position_pct = (
            getattr(s, "aggressive_position_pct", 0.06)
            if aggressive_mode else getattr(s, "rollback_position_pct", getattr(s, "max_position_pct", 0.10))
        )

        if (not is_pyramid) and open_positions >= max_open_positions:
            return EntryRiskDecision(False, f"max_open_positions={max_open_positions}", Decimal("0"))

        if long_exposure_pct >= s.max_long_exposure_pct:
            return EntryRiskDecision(False, f"max_long_exposure_pct={s.max_long_exposure_pct}", Decimal("0"))

        cooled, why = self._loss_cooldown_active(mode)
        if cooled:
            return EntryRiskDecision(False, why, Decimal("0"))

        if entry_price <= 0 or total_equity_usdt <= 0:
            return EntryRiskDecision(False, "invalid_price_or_equity", Decimal("0"))

        # Risk-per-trade sizing: risk 1% of portfolio at the configured stop loss.
        risk_usdt = total_equity_usdt * Decimal(str(s.risk_per_trade_pct))
        stop_pct = Decimal(str(s.stop_loss_pct))
        risk_based_notional = (risk_usdt / stop_pct) if stop_pct > 0 else Decimal("0")

        # Kelly-aware cap + per-position cap to avoid oversized entries.
        kelly_cap_notional = total_equity_usdt * Decimal(str(s.kelly_fraction_cap))
        position_cap_notional = total_equity_usdt * Decimal(str(position_pct))

        if is_pyramid:
            pyramid_notional = (current_position_notional or Decimal("0")) * Decimal(str(getattr(s, "pyramid_add_fraction", 0.50)))
            notional = min(pyramid_notional, kelly_cap_notional, position_cap_notional)
        else:
            notional = min(risk_based_notional, kelly_cap_notional, position_cap_notional)
        if notional <= 0:
            return EntryRiskDecision(False, "non_positive_notional", Decimal("0"))

        return EntryRiskDecision(True, "ok", notional)

    def _loss_cooldown_active(self, mode: str) -> tuple[bool, str]:
        s = get_settings()
        trades = [t for t in storage.closed_trades(limit=100) if t.get("mode") == mode]
        if not trades:
            return False, "ok"

        streak = 0
        latest_loss_ts: datetime | None = None
        for t in trades:
            pnl = Decimal(str(t.get("pnl", 0)))
            if pnl < 0:
                streak += 1
                if latest_loss_ts is None:
                    try:
                        latest_loss_ts = datetime.fromisoformat(str(t.get("exit_ts")))
                        if latest_loss_ts.tzinfo is None:
                            latest_loss_ts = latest_loss_ts.replace(tzinfo=timezone.utc)
                    except ValueError:
                        latest_loss_ts = None
                continue
            break

        if streak < s.loss_streak_pause_count:
            return False, "ok"

        if latest_loss_ts is None:
            return True, f"loss_streak_pause:streak={streak}"

        resume_at = latest_loss_ts + timedelta(minutes=s.loss_streak_pause_minutes)
        if datetime.now(timezone.utc) < resume_at:
            return True, f"loss_streak_pause:streak={streak}:resume_at={resume_at.isoformat()}"

        return False, "ok"
