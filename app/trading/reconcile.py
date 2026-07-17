"""Portfolio reconciliation helpers to keep local positions aligned with exchange balances."""
from __future__ import annotations

from decimal import Decimal

from app.logging_setup import get_logger
from app.storage import storage
from app.trading.portfolio import portfolio_snapshot

log = get_logger(__name__)


async def reconcile_positions(mode: str) -> dict[str, int]:
    """Close stale positions when holdings no longer exist on the exchange/paper ledger."""
    snap = await portfolio_snapshot(mode=mode)
    balances = {k: Decimal(str(v)) for k, v in snap["all_balances"].items()}
    closed = 0
    kept = 0

    for pos in [p for p in storage.all_positions() if p["mode"] == mode]:
        symbol = str(pos["symbol"])
        base = symbol.removesuffix("USDT")
        have = balances.get(base, Decimal("0"))
        if have <= 0:
            try:
                storage.close_position(symbol=symbol, mode=mode, exit_price=Decimal(str(pos["entry_price"])))
                closed += 1
                log.warning("reconcile closed stale position: %s mode=%s", symbol, mode)
            except Exception as e:  # noqa: BLE001
                logger = log
                logger.exception(f"Trade execution failure: {e}")
                raise
        else:
            kept += 1

    return {"closed": closed, "kept": kept}
