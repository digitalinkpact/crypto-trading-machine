"""Startup safety verification before trading loops are started."""
from __future__ import annotations

from app.config import get_settings
from app.exchange import BinanceUSClient
from app.logging_setup import get_logger
from app.storage import storage
from app.trading import watchdog
from app.trading.reconcile import reconcile_positions

log = get_logger(__name__)


async def verify_before_trading() -> None:
    """Verify local/exchange state and halt new entries on any discrepancy."""
    mode = "paper" if get_settings().paper_trading else "live"
    prior_state = storage.kv_get("autopilot_state") or {}
    if prior_state.get("running") and not prior_state.get("starting_balance_usdt"):
        storage.kv_set(
            "baseline_unavailable",
            {
                "active": True,
                "reason": "startup restored a running autopilot without a valid baseline",
                "since": prior_state.get("started_at"),
            },
        )
        log.critical(
            "startup found no persisted portfolio baseline; new BUY entries remain blocked"
        )
    prior_entry_status = storage.kv_get("entry_status") or {}
    prior_reasons = prior_entry_status.get("reasons", []) if isinstance(prior_entry_status, dict) else []
    legacy_drawdown_halt = (
        "DRAWDOWN BREAKER" in str(prior_state.get("last_error", ""))
        or "drawdown_circuit_breaker" in prior_reasons
    )
    persisted_drawdown_halt = storage.kv_get("drawdown_halt") or {}
    if legacy_drawdown_halt and not persisted_drawdown_halt.get("active"):
        storage.kv_set(
            "drawdown_halt",
            {
                "active": True,
                "legacy": True,
                "triggered_at": prior_entry_status.get("checked_at"),
                "reason": "legacy drawdown breaker state preserved during migration",
                "requires_operator_recovery": True,
            },
        )
        log.critical(
            "startup preserved legacy drawdown halt; explicit operator recovery "
            "is required before new BUY entries can resume"
        )
    unresolved_order = storage.kv_get("order_outcome_unknown")
    if unresolved_order:
        watchdog.trigger_emergency_halt(
            "startup found unresolved order_outcome_unknown; verify the exchange order "
            "by client ID before allowing new entries",
            level="order_outcome_unknown",
        )
        return
    try:
        result = await reconcile_positions(mode=mode)
        if result.get("mismatched", 0) or result.get("unknown_orders", 0):
            watchdog.trigger_emergency_halt(
                f"startup reconciliation found discrepancies: {result}",
                level="new_entries_blocked",
            )
        if mode == "live":
            orders = await BinanceUSClient().open_orders()
            if orders:
                watchdog.trigger_emergency_halt(
                    f"startup found {len(orders)} unexpected open orders",
                    level="new_entries_blocked",
                )
    except Exception as exc:  # noqa: BLE001
        log.critical("startup reconciliation failed; blocking new entries: %s", exc)
        watchdog.trigger_emergency_halt(
            f"startup reconciliation failed: {exc}",
            level="new_entries_blocked",
        )
