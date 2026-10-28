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
            settings = get_settings()
            expected_stops = {}
            if getattr(settings, "exchange_native_stops_enabled", False):
                from app.trading.protective_stops import stop_coid

                for position in storage.all_positions():
                    symbol = str(position.get("symbol", "")).upper()
                    if position.get("mode") != "live" or not settings.exchange_stop_allowed(symbol):
                        continue
                    coid = stop_coid(
                        settings.exchange_stop_coid_prefix,
                        symbol,
                        str(position.get("entry_ts") or ""),
                    )
                    expected_stops[coid] = position

            active_stops = set()
            unexpected_orders = []
            for order in orders:
                coid = str(order.get("clientOrderId", ""))
                position = expected_stops.get(coid)
                if position is None:
                    unexpected_orders.append(order)
                    continue
                try:
                    order_qty = float(order.get("origQty", "0"))
                    position_qty = float(position.get("qty", "0"))
                except (TypeError, ValueError):
                    unexpected_orders.append(order)
                    continue
                if (
                    str(order.get("side", "")).upper() == "SELL"
                    and str(order.get("type", "")).upper() == "STOP_LOSS"
                    and str(order.get("status", "")).upper() in {"NEW", "PARTIALLY_FILLED"}
                    and 0 < order_qty <= position_qty
                ):
                    active_stops.add(coid)
                else:
                    unexpected_orders.append(order)

            missing_stops = set(expected_stops) - active_stops
            if unexpected_orders or missing_stops:
                watchdog.trigger_emergency_halt(
                    "startup found "
                    f"{len(unexpected_orders)} unexpected open order(s) and "
                    f"{len(missing_stops)} missing/inactive protective stop(s)",
                    level="new_entries_blocked",
                )
    except Exception as exc:  # noqa: BLE001
        log.critical("startup reconciliation failed; blocking new entries: %s", exc)
        watchdog.trigger_emergency_halt(
            f"startup reconciliation failed: {exc}",
            level="new_entries_blocked",
        )
