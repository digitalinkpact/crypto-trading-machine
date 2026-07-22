#!/bin/bash
# Real-time trade execution monitor
# Shows: executed trades, positions held, rejection reasons

TARGET_POSITIONS=10

while true; do
  cd /workspaces/crypto-trading-machine
  
  python3 << 'PYSCRIPT'
import asyncio
from collections import defaultdict
from app.storage import storage
from app.exchange import BinanceUSClient
from datetime import datetime, timezone

async def monitor():
    trades = storage.recent_trade_audit(limit=500)
    buy_exec = sum(1 for t in trades if t.get("signal") == "BUY" and t.get("execution_attempted"))
    sell_exec = sum(1 for t in trades if t.get("signal") == "SELL" and t.get("execution_attempted"))
    
    client = BinanceUSClient()
    account = await client.account()
    balances = account.get("balances", [])
    positions = sum(1 for b in balances if (float(b["free"]) + float(b["locked"])) > 0 and b["asset"] != "USDT")
    
    rejections = defaultdict(int)
    for t in trades:
        if not t.get("execution_attempted"):
            rejections[t.get("final_outcome", "unknown")] += 1
    
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    
    # Status indicator
    trade_status = "✅" if (buy_exec + sell_exec) > 0 else "⏳"
    pos_status = "✅" if positions == 10 else f"⚠️ " if positions < 10 else "❌"
    
    print(f"\n[{ts}] TRADE MONITOR")
    print(f"  {trade_status} Executions: {buy_exec} BUY + {sell_exec} SELL")
    print(f"  {pos_status} Positions:   {positions}/10 held")
    print(f"  Rejections (top 3):")
    for reason, count in sorted(rejections.items(), key=lambda x: -x[1])[:3]:
        print(f"    - {reason:20} {count}x")

asyncio.run(monitor())
PYSCRIPT

  sleep 60
done
