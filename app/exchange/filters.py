"""Symbol filters loaded once from Binance.US `exchangeInfo`.

Used to round order quantities to the LOT_SIZE step and verify MIN_NOTIONAL
before submitting orders (live OR paper, so behavior matches at switchover).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional

from binance.spot import Spot  # type: ignore[import-untyped]

from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)


def _plain(qty: Decimal) -> Decimal:
    """Strip trailing zeros but never return scientific notation.

    `Decimal.normalize()` yields e.g. Decimal('5E+5') for 500000, whose str()
    is "5E+5" — Binance rejects that with -1100 "illegal characters". For any
    value with a positive exponent we requantize to exponent 0 instead.
    """
    n = qty.normalize()
    return n.quantize(Decimal("1")) if n.as_tuple().exponent > 0 else n


class SymbolFilters:
    def __init__(self) -> None:
        self._info: dict[str, dict[str, Any]] = {}
        self._loaded = False

    async def load(self) -> None:
        if self._loaded:
            return
        s = get_settings()
        spot = Spot(base_url=s.binance_base_url)
        try:
            data = await asyncio.to_thread(spot.exchange_info)
        except Exception as exc:  # noqa: BLE001
            log.warning("exchange_info load failed (%s); filters disabled", exc)
            return
        for sym in data.get("symbols", []):
            entry = {"status": sym.get("status")}
            for f in sym.get("filters", []):
                t = f.get("filterType")
                if t == "LOT_SIZE":
                    entry["step_size"] = Decimal(f["stepSize"])
                    entry["min_qty"] = Decimal(f["minQty"])
                elif t in ("MIN_NOTIONAL", "NOTIONAL"):
                    entry["min_notional"] = Decimal(
                        f.get("minNotional") or f.get("notional") or "0"
                    )
            self._info[sym["symbol"]] = entry
        self._loaded = True
        log.info("loaded filters for %d symbols", len(self._info))

    def is_listed(self, symbol: str) -> bool:
        if not self._loaded:
            return True  # don't block when filters unavailable
        info = self._info.get(symbol)
        return bool(info and info.get("status") == "TRADING")

    def round_qty(self, symbol: str, qty: Decimal) -> Decimal:
        info = self._info.get(symbol) or {}
        step: Optional[Decimal] = info.get("step_size")
        if step and step > 0:
            qty = (qty / step).quantize(Decimal("1"), rounding=ROUND_DOWN) * step
            # Re-quantize to the step's own exponent so the result keeps a plain
            # fixed-point representation (e.g. step=1 → "500000", not "5E+5").
            # Binance rejects scientific notation with -1100 "illegal characters".
            qty = qty.quantize(step) if step >= 1 else qty
        return _plain(qty)

    def meets_min(self, symbol: str, qty: Decimal, price: Decimal) -> bool:
        info = self._info.get(symbol) or {}
        min_qty: Optional[Decimal] = info.get("min_qty")
        min_notional: Optional[Decimal] = info.get("min_notional")
        if min_qty and qty < min_qty:
            return False
        if min_notional and (qty * price) < min_notional:
            return False
        return True


filters = SymbolFilters()
