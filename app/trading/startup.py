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
