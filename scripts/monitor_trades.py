#!/usr/bin/env python3
"""Monitor trade executions in real-time."""
import asyncio
import sys
from datetime import datetime, timezone
from collections import deque
from app.storage import storage
from app.logging_setup import get_logger

log = get_logger(__name__)

# Track trades from the audit log
last_id = 0
execution_count = 0
recent_trades = deque(maxlen=20)


async def monitor_trades():
    """Monitor execution of BUY/SELL orders."""
    global last_id, execution_count
    
    while True:
        try:
            # Fetch recent audit records
            trades = storage.recent_trade_audit(limit=100)
            
            for trade in trades:
                trade_id = trade.get("id", 0)
                if trade_id > last_id:
                    # New trade record
                    symbol = trade.get("symbol", "?")
                    signal = trade.get("signal", "?")
                    outcome = trade.get("final_outcome", "?")
                    attempted = trade.get("execution_attempted", False)
                    timestamp = trade.get("timestamp", "")
                    
                    status = "✅ EXECUTED" if attempted else f"⚠️  REJECTED ({outcome})"
                    
                    trade_info = f"[{symbol}] {signal:4} {status}"
                    recent_trades.append(trade_info)
                    
                    if attempted:
                        execution_count += 1
                        log.info(f"TRADE EXECUTED: {symbol} {signal}")
                    
                    last_id = max(last_id, trade_id)
            
            # Display summary
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            print(f"\n[{timestamp}] Executions: {execution_count}")
            
            if recent_trades:
                print("  Recent activity:")
                for trade in list(recent_trades)[-5:]:
                    print(f"    {trade}")
            else:
                print("  (waiting for first tick...)")
            
            await asyncio.sleep(30)
            
        except Exception as e:
            log.error(f"Monitor error: {e}")
            await asyncio.sleep(30)


if __name__ == "__main__":
    print("=" * 70)
    print("TRADE EXECUTION MONITOR")
    print("=" * 70)
    print("Monitoring orders every 30 seconds...\n")
    
    try:
        asyncio.run(monitor_trades())
    except KeyboardInterrupt:
        print(f"\n\nMonitor stopped. Total executions: {execution_count}")
        sys.exit(0)
