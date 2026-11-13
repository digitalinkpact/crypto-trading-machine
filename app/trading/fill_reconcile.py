"""Independent Binance.US fill reconciliation (read-only against the exchange).

The order path books a fill from the order-placement response. This module
re-verifies every live order afterwards from two separate exchange sources:

  1. the order itself, queried by our idempotency key (newClientOrderId), and
  2. the account trade list (`myTrades`) for that exchange orderId.

A fill counts as verified only when the trade records add up to the order's
executedQty. An API call that succeeded proves nothing about a fill. Exchange
calls are queries only: this module never places, cancels, or amends orders,
and never edits `orders` or `positions`. It writes to `fill_reconciliation`
and moves an `order_intents` row to an exchange-confirmed terminal status.

Stages written to fill_reconciliation.stage:
  UNVERIFIED                 lookup failed (network, rate limit, missing trades)
  NOT_ON_EXCHANGE            exchange confirms the client order id never landed
  ACCEPTED                   exchange holds the order, nothing executed yet
  PARTIALLY_FILLED           order live with partial execution
  FILLS_PENDING              executedQty > 0 but trade records don't add up yet
  FILLED_VERIFIED            FILLED and trade records sum to executedQty
  PARTIAL_TERMINAL_VERIFIED  CANCELED/EXPIRED after partial, trades verified
  CANCELED / EXPIRED         terminal with zero execution
  EXCHANGE_REJECTED          Binance.US rejected the order
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from app.logging_setup import get_logger
from app.storage import storage

log = get_logger(__name__)

TERMINAL_STAGES = frozenset({
    "FILLED_VERIFIED", "PARTIAL_TERMINAL_VERIFIED", "CANCELED", "EXPIRED",
    "EXCHANGE_REJECTED", "NOT_ON_EXCHANGE",
})
_TERMINAL_EXCHANGE = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}
# Binance can briefly answer "unknown order" right after submission; only treat
# absence as final once the intent is older than this.
ABSENT_GRACE = timedelta(minutes=5)
# "Unknown order" is only proof the order never landed while it is well inside
# the exchange's order-history retention; older lookups stay UNVERIFIED.
ABSENT_PROOF_MAX_AGE = timedelta(days=30)
QTY_TOLERANCE = Decimal("1e-8")


def _dec(value: Any) -> Decimal:
    try:
        return Decimal(str(value if value not in (None, "") else "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _parse_ts(value: Any) -> Optional[datetime]:
    try:
        ts = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def summarize_trades(trades: list[dict], order_id: Any) -> dict:
    """Sum unique trade records belonging to `order_id`.

    Duplicate records (same trade id, e.g. a retried or paginated response)
    are counted once; records for a different orderId are ignored.
    """
    seen: set = set()
    qty = Decimal("0")
    quote = Decimal("0")
    commission: dict[str, Decimal] = {}
    for trade in trades or []:
        if not isinstance(trade, dict):
            continue
        if trade.get("orderId") is not None and str(trade.get("orderId")) != str(order_id):
            continue
        trade_id = trade.get("id")
        key = trade_id if trade_id is not None else (trade.get("time"), trade.get("qty"), trade.get("price"))
        if key in seen:
            continue
        seen.add(key)
        trade_qty = _dec(trade.get("qty"))
        qty += trade_qty
        quote += _dec(trade.get("quoteQty")) or trade_qty * _dec(trade.get("price"))
        asset = str(trade.get("commissionAsset") or "?")
        commission[asset] = commission.get(asset, Decimal("0")) + _dec(trade.get("commission"))
    return {"count": len(seen), "qty": qty, "quote": quote,
            "commission": {asset: str(amount) for asset, amount in commission.items()}}


def _candidates(mode: str, *, orders_lookback: timedelta, limit: int) -> list[dict]:
    intents = storage.order_intents_for_reconciliation(mode, limit=limit)
    by_coid = {intent["client_order_id"]: dict(intent, source="intent") for intent in intents}
    # Orders booked before order_intents existed (or whose intent was resolved)
    # still deserve one independent check.
    cutoff = (datetime.now(timezone.utc) - orders_lookback).isoformat()
    for order in storage.recent_orders(limit=limit, mode=mode):
        coid = order.get("client_order_id")
        if not coid or coid in by_coid or str(order.get("ts") or "") < cutoff:
            continue
        existing = storage.fill_reconciliation(coid)
        if existing and existing["stage"] in TERMINAL_STAGES and not existing.get("discrepancy"):
            continue
        by_coid[coid] = {"client_order_id": coid, "mode": mode, "symbol": order["symbol"],
                         "side": order["side"], "status": "RECORDED", "updated_at": order["ts"],
                         "source": "orders"}
    return list(by_coid.values())


async def reconcile_order(candidate: dict, client, *, now: Optional[datetime] = None) -> dict:
    """Verify one client order id against Binance.US and persist the result."""
    now = now or datetime.now(timezone.utc)
    coid = candidate["client_order_id"]
    mode = candidate["mode"]
    symbol = candidate["symbol"]
    local = storage.order_by_client_id(mode, coid)
    local_qty = _dec(local.get("filled_qty") if local.get("filled_qty") is not None else local.get("qty")) if local else None
    row: dict[str, Any] = {
        "client_order_id": coid, "mode": mode, "symbol": symbol, "side": candidate.get("side"),
        "local_qty": str(local_qty) if local_qty is not None else None,
        "detail": {"intent_status": candidate.get("status"), "source": candidate.get("source")},
    }
    discrepancy: Optional[str] = None

    outcome, raw = await client.get_order_by_client_id(symbol, coid)
    if outcome == "inconclusive" or (outcome == "found" and not isinstance(raw, dict)):
        row.update(stage="UNVERIFIED", detail={**row["detail"], "reason": "order lookup inconclusive"})
        storage.upsert_fill_reconciliation(row)
        return row

    if outcome == "confirmed_absent":
        age = now - (_parse_ts(candidate.get("updated_at")) or now)
        if local is not None:
            discrepancy = "local_fill_without_exchange_order"
        if age < ABSENT_GRACE and discrepancy is None:
            row.update(stage="UNVERIFIED", detail={**row["detail"], "reason": "absent within grace window"})
        elif age > ABSENT_PROOF_MAX_AGE and discrepancy is None:
            row.update(stage="UNVERIFIED",
                       detail={**row["detail"], "reason": "absent but older than order-history retention margin"})
        else:
            row["stage"] = "NOT_ON_EXCHANGE"
            if discrepancy is None and candidate.get("source") == "intent":
                storage.resolve_order_intent(coid, "NOT_ON_EXCHANGE")
        row["discrepancy"] = discrepancy
        storage.upsert_fill_reconciliation(row)
        return row

    status = str(raw.get("status") or "").upper()
    order_id = raw.get("orderId")
    executed = _dec(raw.get("executedQty"))
    row.update(exchange_order_id=str(order_id) if order_id is not None else None,
               exchange_status=status, executed_qty=str(executed))

    trades_ok = True
    if executed > 0:
        try:
            trades = await client.my_trades(symbol, order_id)
        except Exception as exc:  # noqa: BLE001 - network failure must not be read as "no fill"
            trades, trades_ok = [], False
            row["detail"] = {**row["detail"], "trades_error": type(exc).__name__}
        summary = summarize_trades(trades, order_id)
        row.update(trade_qty=str(summary["qty"]), trade_quote_qty=str(summary["quote"]),
                   trade_count=summary["count"], commission=summary["commission"])
        trades_ok = trades_ok and abs(summary["qty"] - executed) <= QTY_TOLERANCE

    if status == "REJECTED":
        stage = "EXCHANGE_REJECTED"
    elif status == "NEW" and executed == 0:
        stage = "ACCEPTED"
    elif status == "PARTIALLY_FILLED" or (status == "NEW" and executed > 0):
        stage = "PARTIALLY_FILLED"
    elif status in _TERMINAL_EXCHANGE and executed > 0:
        if not trades_ok:
            stage = "FILLS_PENDING" if "trades_error" not in row["detail"] else "UNVERIFIED"
        else:
            stage = "FILLED_VERIFIED" if status == "FILLED" else "PARTIAL_TERMINAL_VERIFIED"
    elif status in ("CANCELED", "EXPIRED", "EXPIRED_IN_MATCH"):
        stage = "EXPIRED" if status.startswith("EXPIRED") else "CANCELED"
    else:
        stage = "ACCEPTED"
    row["stage"] = stage

    # Any exchange-executed quantity (open partial, trades pending, or terminal)
    # must already be on our book; otherwise coins are untracked.
    if executed > 0 and stage != "UNVERIFIED":
        if local is None:
            discrepancy = "exchange_fill_not_recorded"
        elif abs(local_qty - executed) > QTY_TOLERANCE:
            discrepancy = f"qty_mismatch local={local_qty} exchange={executed}"
    elif stage in ("CANCELED", "EXPIRED", "EXCHANGE_REJECTED") and local is not None:
        discrepancy = "local_fill_without_exchange_execution"
    row["discrepancy"] = discrepancy

    if stage in TERMINAL_STAGES and discrepancy is None and candidate.get("source") == "intent":
        storage.resolve_order_intent(coid, "EXPIRED" if status == "EXPIRED_IN_MATCH" else status)
    storage.upsert_fill_reconciliation(row)
    return row


async def reconcile_fills(
    mode: str = "live",
    *,
    client=None,
    halt_on_discrepancy: bool = True,
    orders_lookback: timedelta = timedelta(days=7),
    limit: int = 200,
) -> dict:
    """Reconcile unresolved/unverified live orders. Paper mode has no exchange."""
    if mode != "live":
        return {"mode": mode, "skipped": "no exchange in paper mode", "checked": 0}
    if client is None:
        from app.exchange import BinanceUSClient

        client = BinanceUSClient()
    stages: dict[str, int] = {}
    discrepancies: list[dict] = []
    candidates = _candidates(mode, orders_lookback=orders_lookback, limit=limit)
    for candidate in candidates:
        try:
            result = await reconcile_order(candidate, client)
        except Exception as exc:  # noqa: BLE001 - one bad order must not hide the rest
            log.error("fill reconciliation failed for coid=%s: %s", candidate.get("client_order_id"), exc)
            stages["ERROR"] = stages.get("ERROR", 0) + 1
            continue
        stages[result["stage"]] = stages.get(result["stage"], 0) + 1
        if result.get("discrepancy"):
            discrepancies.append({key: result.get(key) for key in (
                "client_order_id", "symbol", "side", "exchange_order_id", "exchange_status",
                "executed_qty", "local_qty", "discrepancy")})
    summary = {"mode": mode, "checked": len(candidates), "stages": stages,
               "discrepancies": discrepancies,
               "checked_at": datetime.now(timezone.utc).isoformat()}
    storage.kv_set("fill_reconciliation_last_run", summary)
    if discrepancies:
        log.critical("fill reconciliation discrepancies: %s", discrepancies)
        if halt_on_discrepancy:
            from app.trading import watchdog

            watchdog.trigger_emergency_halt(
                f"fill reconciliation found {len(discrepancies)} exchange/ledger discrepancies",
                level="new_entries_blocked",
            )
    return summary
