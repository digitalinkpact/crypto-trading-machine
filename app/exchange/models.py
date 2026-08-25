"""Order / market data domain models — exchange-agnostic shapes."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LOSS_LIMIT = "STOP_LOSS_LIMIT"
    TAKE_PROFIT_LIMIT = "TAKE_PROFIT_LIMIT"


class OrderStatus(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    DRY_RUN = "DRY_RUN"


class Order(BaseModel):
    model_config = ConfigDict(frozen=True)

    symbol: str
    side: OrderSide
    type: OrderType
    quantity: Decimal
    price: Optional[Decimal] = None
    client_order_id: str = Field(..., description="Idempotency key — newClientOrderId")
    status: OrderStatus = OrderStatus.NEW
    exchange_order_id: Optional[str] = None
    submitted_at: Optional[datetime] = None
    filled_quantity: Decimal = Decimal("0")
    avg_fill_price: Optional[Decimal] = None
    # Actual commission Binance charged on this order (summed across fills),
    # only populated when every fill's commission was paid in the same asset
    # — see app/exchange/client.py `_extract_commission`. None means the real
    # commission couldn't be determined unambiguously; callers must fall back
    # to a modeled fee estimate rather than fabricate a number.
    commission: Optional[Decimal] = None
    commission_asset: Optional[str] = None
    raw: Optional[dict] = None
