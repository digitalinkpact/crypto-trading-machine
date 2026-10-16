"""Startup safety verification before trading loops are started."""
from __future__ import annotations

from app.config import get_settings
from app.exchange import BinanceUSClient
from app.logging_setup import get_logger
from app.storage import storage
from app.trading import watchdog
from app.trading.reconcile import reconcile_positions

log = get_logger(__name__)

# Mutable safety flags read by Autopilot._final_live_buy_safety_check. That
# guard rejects a BUY when any of these kv rows is missing ("safety state
# unknown"), which is the right fail-closed default — but a brand-new DB has
# none of them, so every live BUY would be rejected forever. Each row is
# written ONLY when absent; an existing (possibly active) halt is never touched.
SAFETY_STATE_KEYS: tuple[str, ...] = (
    "drawdown_halt",
    "emergency_halt",
    "tick_protection",
    "entry_status",
)


def ensure_safety_state_initialized() -> list[str]:
    """Seed absent safety-state kv rows as inactive. Returns the keys seeded."""
    seeded: list[str] = []
    for key in SAFETY_STATE_KEYS:
        existing = storage.kv_get(key)
        if isinstance(existing, dict):
            continue
        payload: dict = {"active": False, "initialized_at_startup": True}
        if key == "entry_status":
            payload.update({"entry_halted": False, "reasons": [], "system_offline": False})
        storage.kv_set(key, payload)
        seeded.append(key)
    if seeded:
        log.info("startup seeded inactive safety state for %s", seeded)
    return seeded


async def verify_before_trading() -> None:
    """Verify local/exchange state and halt new entries on any discrepancy."""
    ensure_safety_state_initialized()
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
