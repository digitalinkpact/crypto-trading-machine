"""Centralized trade audit logging for traceable execution decisions."""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from app.logging_setup import get_logger
from app.storage import storage

log = get_logger(__name__)


class TradeAuditLogger:
    """Writes structured trade execution audit rows and emits log entries."""

    def log_event(
        self,
        *,
        mode: str,
        symbol: str,
        signal: str,
        confidence: Optional[float] = None,
        risk_passed: Optional[bool] = None,
        position_exists: Optional[bool] = None,
        available_balance: Optional[Decimal] = None,
        min_notional_passed: Optional[bool] = None,
        execution_attempted: bool = False,
        binance_response: str = "",
        exception: Optional[str] = None,
        final_outcome: str,
        detail: Optional[dict] = None,
    ) -> None:
        storage.record_trade_audit(
            mode=mode,
            symbol=symbol,
            signal=signal,
            confidence=confidence,
            risk_passed=risk_passed,
            position_exists=position_exists,
            available_balance=available_balance,
            min_notional_passed=min_notional_passed,
            execution_attempted=execution_attempted,
            binance_response=binance_response,
            exception=exception,
            final_outcome=final_outcome,
            detail=detail,
        )
        log.info(
            "[TICK] %s signal=%s conf=%s risk=%s position_exists=%s balance=%s min_notional=%s attempted=%s response=%s outcome=%s",
            symbol,
            signal,
            confidence,
            risk_passed,
            position_exists,
            available_balance,
            min_notional_passed,
            execution_attempted,
            binance_response,
            final_outcome,
        )


trade_audit_logger = TradeAuditLogger()
