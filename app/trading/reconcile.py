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
         of it anymore (stale book row) -> report the discrepancy.
    2. The exchange/paper ledger holds a real balance the DB has NO position
       row for at all -> the position is completely unmanaged: no stop-loss,
       no take-profit, no trailing stop, because the risk-gate loop only ever
       looks at `storage.all_positions()`. This happened for real (ZEC/ONDO,
       2026-08-03): both sat unprotected for days before being noticed and
       manually reseeded. Any holding above `UNTRACKED_MIN_VALUE_USDT` is now
    never auto-adopted: reconciliation is read-only and cannot fabricate an
    entry price. The watchdog halt blocks new entries until an operator
    authorizes recovery.
    """
    snap = await portfolio_snapshot(mode=mode)
    balances = {k: Decimal(str(v)) for k, v in snap["all_balances"].items()}
    open_positions = [p for p in storage.all_positions() if p["mode"] == mode]
    closed = 0
    kept = 0
    mismatched = 0

    for pos in open_positions:
        symbol = str(pos["symbol"])
        base = symbol.removesuffix("USDT")
        have = balances.get(base, Decimal("0"))
        if have <= 0:
            mismatched += 1
            log.critical("reconcile found stale local position: %s mode=%s", symbol, mode)
        else:
            book_qty = Decimal(str(pos.get("qty") or 0))
            if book_qty != have:
                mismatched += 1
                log.critical(
                    "reconcile quantity mismatch: %s mode=%s local=%s exchange=%s",
                    symbol, mode, book_qty, have,
                )
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
        mismatched += 1
        log.critical(
            "reconcile found untracked %s holding (qty=%s value=%.2f mode=%s) — "
            "read-only reconciliation will not fabricate an entry price",
            symbol, holding["qty"], value, mode,
        )
        try:
            from app.trading import watchdog  # local import: avoid cycle

            watchdog.trigger_emergency_halt(
                f"untracked {mode} position detected for {symbol}",
                level="new_entries_blocked",
            )
        except Exception as halt_exc:  # noqa: BLE001
            log.error("failed to trigger emergency halt for reconcile mismatch: %s", halt_exc)

    return {"closed": closed, "kept": kept, "adopted": adopted, "mismatched": mismatched}
