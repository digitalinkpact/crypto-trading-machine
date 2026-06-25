#!/usr/bin/env python3
"""
Simulate a complete trade lifecycle: BUY, HOLD, SELL execution.
"""
import asyncio
import sys
from pathlib import Path
from decimal import Decimal

sys.path.insert(0, str(Path(__file__).parent.parent))

async def main():
    from app.config import get_settings, Timeframe
    from app.storage import storage
    from app.trading.paper import paper_exchange
    from app.exchange import OrderSide
    
    print("\n" + "="*80)
    print("SIMULATE TRADE LIFECYCLE")
    print("="*80 + "\n")
    
    settings = get_settings()
    print(f"[Setup]")
    print(f"  Mode: {'paper' if settings.paper_trading else 'live'}")
    print(f"  DRY_RUN: {settings.dry_run}")
    print(f"  Min confidence: {settings.min_signal_confidence}")
    
    # 1. Seed paper exchange
    print(f"\n[1] Seed paper trading account...")
    try:
        paper_exchange.ensure_seeded()
        snap = await paper_exchange.snapshot()
        print(f"✓ Paper account ready")
        print(f"  Total: ${snap['total_usdt']:.2f}")
        print(f"  Cash: ${snap['usdt_cash']:.2f}")
    except Exception as e:
        print(f"✗ Seeding failed: {e}")
        return
    
    # 2. Place a BUY order
    print(f"\n[2] Place a BUY order...")
    try:
        order = await paper_exchange.place_order(
            symbol="BTCUSDT",
            side=OrderSide.BUY,
            quantity=Decimal("0.01"),
            agents=["test"],
            client_order_id="test-buy-001"
        )
        print(f"✓ BUY order placed")
        print(f"  Symbol: {order.symbol}")
        print(f"  Qty: {order.quantity}")
        print(f"  Status: {order.status}")
    except Exception as e:
        print(f"✗ BUY placement failed: {e}")
        return
    
    # 3. Check positions
    print(f"\n[3] Check open positions...")
    try:
        positions = [p for p in storage.all_positions()
                    if p["mode"] == ("paper" if settings.paper_trading else "live")]
        print(f"✓ Open positions: {len(positions)}")
        for p in positions[:3]:
            print(f"  - {p['symbol']}: qty={p['quantity']} entry={p['entry_price']}")
    except Exception as e:
        print(f"✗ Position check failed: {e}")
    
    # 4. Check updated balances
    print(f"\n[4] Check updated balances...")
    try:
        snap = await paper_exchange.snapshot()
        print(f"✓ Portfolio updated")
        print(f"  Total: ${snap['total_usdt']:.2f}")
        print(f"  Cash: ${snap['usdt_cash']:.2f}")
        print(f"  Holdings: {len(snap['holdings'])}")
        for h in snap['holdings'][:3]:
            print(f"    - {h['asset']}: {h['qty']} @ ${h['price_usdt']:.2f} = ${h['value_usdt']:.2f}")
    except Exception as e:
        print(f"✗ Snapshot failed: {e}")
    
    # 5. Place a SELL order
    print(f"\n[5] Place a SELL order...")
    try:
        order = await paper_exchange.place_order(
            symbol="BTCUSDT",
            side=OrderSide.SELL,
            quantity=Decimal("0.01"),
            agents=["test"],
            client_order_id="test-sell-001"
        )
        print(f"✓ SELL order placed")
        print(f"  Symbol: {order.symbol}")
        print(f"  Qty: {order.quantity}")
        print(f"  Status: {order.status}")
    except Exception as e:
        print(f"✗ SELL placement failed: {e}")
    
    # 6. Check final state
    print(f"\n[6] Final state...")
    try:
        snap = await paper_exchange.snapshot()
        positions = [p for p in storage.all_positions()
                    if p["mode"] == ("paper" if settings.paper_trading else "live")]
        print(f"✓ Final snapshot")
        print(f"  Total: ${snap['total_usdt']:.2f}")
        print(f"  Cash: ${snap['usdt_cash']:.2f}")
        print(f"  Holdings: {len(snap['holdings'])}")
        print(f"  Open positions: {len(positions)}")
    except Exception as e:
        print(f"✗ Final check failed: {e}")
    
    print("\n" + "="*80)
    print("LIFECYCLE COMPLETE")
    print("If you see ✓ throughout, the trading engine works.")
    print("="*80 + "\n")

if __name__ == '__main__':
    asyncio.run(main())
