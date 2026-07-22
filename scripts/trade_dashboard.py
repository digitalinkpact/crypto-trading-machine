#!/usr/bin/env python3
"""Live trade execution dashboard — shows BUYs, SELLs, and rejections."""
import asyncio
import sys
from collections import defaultdict, deque
from datetime import datetime, timezone
from app.storage import storage
from app.logging_setup import get_logger

log = get_logger(__name__)


class TradeMonitor:
    """Track and display trade execution stats."""
    
    def __init__(self):
        self.last_id = 0
        self.buy_executed = 0
        self.sell_executed = 0
        self.rejected_reasons = defaultdict(int)
        self.recent = deque(maxlen=10)
    
    async def run(self):
        """Monitor loop."""
        while True:
            try:
                trades = storage.recent_trade_audit(limit=200)
                
                for trade in reversed(trades):
                    trade_id = trade.get("id", 0)
                    if trade_id <= self.last_id:
                        continue
                    
                    symbol = trade.get("symbol", "?")
                    signal = trade.get("signal", "?")
                    outcome = trade.get("final_outcome", "?")
                    attempted = trade.get("execution_attempted", False)
                    
                    if attempted:
                        if signal == "BUY":
                            self.buy_executed += 1
                        elif signal == "SELL":
                            self.sell_executed += 1
                        log.info(f"[TRADE] {symbol} {signal} executed")
                    else:
                        self.rejected_reasons[outcome] += 1
                    
                    self.recent.append((symbol, signal, attempted, outcome))
                    self.last_id = max(self.last_id, trade_id)
                
                # Display
                ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
                print(f"\n[{ts}] TRADE EXECUTION STATS")
                print(f"  ✅ BUY executed:  {self.buy_executed:3}")
                print(f"  ✅ SELL executed: {self.sell_executed:3}")
                print(f"\n  Top rejection reasons:")
                
                for reason, count in sorted(self.rejected_reasons.items(), key=lambda x: -x[1])[:5]:
                    print(f"    ❌ {reason:20} {count:3}x")
                
                if self.recent:
                    print(f"\n  Recent activity (last 5):")
                    for sym, sig, executed, reason in list(self.recent)[-5:]:
                        status = "✅" if executed else "❌"
                        print(f"    {status} {sym:12} {sig:4}")
                
                await asyncio.sleep(30)
                
            except Exception as e:
                log.error(f"Error: {e}")
                await asyncio.sleep(30)


async def main():
    monitor = TradeMonitor()
    await monitor.run()


if __name__ == "__main__":
    print("=" * 70)
    print("LIVE TRADE EXECUTION DASHBOARD")
    print("=" * 70)
    print("Monitoring trade executions every 30 seconds\n")
    
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\nDashboard stopped.")
        sys.exit(0)
