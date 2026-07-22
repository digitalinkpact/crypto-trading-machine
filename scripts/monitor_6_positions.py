#!/usr/bin/env python3
"""Monitor positions held by trading bot — target 10 open positions."""
import asyncio
import sys
from datetime import datetime, timezone
from app.exchange import BinanceUSClient
from app.logging_setup import get_logger

log = get_logger(__name__)
TARGET = 10


async def monitor_positions():
    """Continuously check position count."""
    client = BinanceUSClient()
    last_count = None
    last_symbols = set()
    
    while True:
        try:
            account = await client.account()
            balances = account.get("balances", [])
            
            positions = []
            for b in balances:
                total = float(b["free"]) + float(b["locked"])
                if total > 0 and b["asset"] != "USDT":
                    positions.append(b["asset"])
            
            count = len(positions)
            symbols = set(p.replace("USDT", "") for p in positions if "USDT" in p or p in ["BTC", "ETH"])
            
            # Only log if count changed
            if count != last_count or symbols != last_symbols:
                timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                status = "✅" if count == TARGET else f"⚠️  {count}/{TARGET}"
                
                print(f"\n[{timestamp}] Position count: {count}/{TARGET} {status}")
                if symbols:
                    print(f"  Held: {', '.join(sorted(symbols))}")
                
                # Alert if over target
                if count > TARGET:
                    log.warning(f"ALERT: {count} positions held (target={TARGET}) — reduce before next entry")
                
                last_count = count
                last_symbols = symbols
            
            await asyncio.sleep(30)
            
        except Exception as e:
            log.error(f"Monitor error: {e}")
            await asyncio.sleep(60)


if __name__ == "__main__":
    print("=" * 70)
    print("POSITION MONITOR — Target: 10 open positions")
    print("=" * 70)
    print("Monitoring every 30 seconds...\n")
    
    try:
        asyncio.run(monitor_positions())
    except KeyboardInterrupt:
        print("\n\nMonitor stopped.")
        sys.exit(0)
