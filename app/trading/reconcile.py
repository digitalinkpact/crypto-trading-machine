"""Portfolio reconciliation helpers to keep local positions aligned with exchange balances."""
from __future__ import annotations

from decimal import Decimal

from app.exchange.symbols import _is_stable_pair
from app.logging_setup import get_logger
from app.storage import storage
from app.trading.portfolio import portfolio_snapshot

log = get_logger(__name__)

# Ignore exchange dust below this USDT value when looking for untracked
# holdings — a few cents of residue isn't a position that needs protecting.
UNTRACKED_MIN_VALUE_USDT = Decimal("5")


async def reconcile_positions(mode: str) -> dict[str, int]:
    """Keep local `positions` rows aligned with real exchange/paper holdings.

    Two independent drift directions are checked:

    1. DB says a position is open, but the exchange/paper ledger holds none
       of it anymore (stale book row) -> close it locally.
    2. The exchange/paper ledger holds a real balance the DB has NO position
       row for at all -> the position is completely unmanaged: no stop-loss,
       no take-profit, no trailing stop, because the risk-gate loop only ever
       looks at `storage.all_positions()`. This happened for real (ZEC/ONDO,
       2026-08-03): both sat unprotected for days before being noticed and
       manually reseeded. Any holding above `UNTRACKED_MIN_VALUE_USDT` is now
       auto-adopted at the current market price so it comes under risk-gate
       protection on the very next cycle — an approximate entry price and a
       stop is far better than no stop at all. If adoption itself fails, that
       is logged CRITICAL and escalated to the emergency-halt watchdog path
       (new entries blocked) since a position may still be unmanaged.
    """
    snap = await portfolio_snapshot(mode=mode)
    balances = {k: Decimal(str(v)) for k, v in snap["all_balances"].items()}
    open_positions = [p for p in storage.all_positions() if p["mode"] == mode]
    closed = 0
    kept = 0

    for pos in open_positions:
        symbol = str(pos["symbol"])
        base = symbol.removesuffix("USDT")
        have = balances.get(base, Decimal("0"))
        if have <= 0:
            try:
                storage.close_position(
                    symbol=symbol, mode=mode, exit_price=Decimal(str(pos["entry_price"])),
                    exit_reason="reconcile_stale",
                )
                closed += 1
                log.warning("reconcile closed stale position: %s mode=%s", symbol, mode)
            except Exception as e:  # noqa: BLE001
                # One bad position must not abort reconciliation of the rest —
                # a raise here used to skip every remaining position in this
                # pass. Log and move on; the next 5-minute cycle retries it.
                log.exception("reconcile failed to close stale position %s mode=%s: %s", symbol, mode, e)
        else:
            kept += 1

    adopted = 0
    tracked_bases = {str(p["symbol"]).removesuffix("USDT") for p in open_positions}
    for holding in snap.get("holdings", []):
        asset = holding.get("asset")
        if not asset or asset in tracked_bases:
            continue
        symbol = f"{asset}USDT"
        if _is_stable_pair(symbol):
            continue
        value = Decimal(str(holding.get("value_usdt") or 0))
        if value < UNTRACKED_MIN_VALUE_USDT:
            continue
        try:
            storage.open_position(
                symbol=symbol,
                mode=mode,
                qty=Decimal(str(holding["qty"])),
                entry_price=Decimal(str(holding["price_usdt"])),
                agents=["reconcile:untracked_exchange_position"],
            )
            adopted += 1
            log.critical(
                "reconcile ADOPTED untracked %s position (qty=%s value=%.2f mode=%s) "
                "— this holding had NO stop-loss/take-profit coverage until now",
                symbol, holding["qty"], value, mode,
            )
        except Exception as e:  # noqa: BLE001
            log.critical(
                "reconcile FAILED to adopt untracked position %s mode=%s value=%.2f — "
                "this position remains UNMANAGED: %s", symbol, mode, value, e,
            )
            try:
                from app.trading import watchdog  # local import: avoid cycle

                watchdog.trigger_emergency_halt(
                    f"failed to reconcile untracked {mode} position {symbol}: {e}",
                    level="new_entries_blocked",
                )
            except Exception as halt_exc:  # noqa: BLE001
                log.error("failed to trigger emergency halt for reconcile failure: %s", halt_exc)

    return {"closed": closed, "kept": kept, "adopted": adopted}
