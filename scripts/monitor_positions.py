#!/usr/bin/env python3
"""Monitor that the bot maintains exactly 6 open positions."""
import asyncio
import json
import time
from datetime import datetime, timezone
from decimal import Decimal

from app.exchange.client import exchange_client
from app.config import get_settings
from app.logging_setup import get_logger

log = get_logger(__name__)
TARGET_POSITIONS = 6


async def get_open_positions() -> list[dict]:
    """Fetch open SPOT positions from Binance.US."""
    try:
        account = await exchange_client.get_account()
        balances = account.get("balances", [])
        # Filter to non-zero balances (positions are held)
        positions = [
            {
                "symbol": b["asset"],
                "free": Decimal(b["free"]),
                "locked": Decimal(b["locked"]),
                "total": Decimal(b["free"]) + Decimal(b["locked"]),
            }
            for b in balances
            if (Decimal(b["free"]) + Decimal(b["locked"])) > 0
            and b["asset"] != "USDT"  # exclude USDT (base currency)
        ]
        return positions
    except Exception as e:
        log.error(f"Failed to fetch positions: {e}")
        return []


async def monitor_loop():
    """Continuously monitor position count."""
    print("\n" + "=" * 80)
    print("POSITION MONITOR — Target: 6 open positions")
    print("=" * 80)
    
    last_count = None
    alert_sent = False
    
    while True:
        try:
            positions = await get_open_positions()
            count = len(positions)
            timestamp = datetime.now(timezone.utc).isoformat()
            
            # Build output
            status = "✅ OK" if count == TARGET_POSITIONS else f"⚠️  {count} != {TARGET_POSITIONS}"
            print(f"\n[{timestamp}] Positions: {count}/{TARGET_POSITIONS} {status}")
            
            if count != last_count:
                print(f"  Held: {', '.join([p['symbol'] for p in positions]) if positions else 'none'}")
                last_count = count
            
            # Alert if drifted from target
            if count < TARGET_POSITIONS and not alert_sent:
                log.warning(
                    f"[POSITION_ALERT] Only {count}/{TARGET_POSITIONS} positions held. "
                    f"Bot may be throttled or have hit risk brakes."
                )
                alert_sent = True
            elif count == TARGET_POSITIONS:
                alert_sent = False
            
            await asyncio.sleep(30)  # Check every 30 seconds
            
        except Exception as e:
            log.error(f"Monitor error: {e}")
            await asyncio.sleep(30)


if __name__ == "__main__":
    print("Starting position monitor...")
    asyncio.run(monitor_loop())
