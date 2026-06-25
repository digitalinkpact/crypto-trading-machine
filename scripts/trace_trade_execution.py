#!/usr/bin/env python3
"""
Trace why trades are not executing. Tests each gate in the autopilot
decision flow and checks if orders are being placed or skipped.
"""
import asyncio
import logging
from pathlib import Path
import sys

# Setup path
sys.path.insert(0, str(Path(__file__).parent.parent))

logging.basicConfig(level=logging.DEBUG, format='%(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def main():
    print("\n" + "="*80)
    print("TRACE: Trade Execution Flow")
    print("="*80)
    
    from app.config import SYMBOLS, TIMEFRAMES, get_settings
    from app.storage import storage
    from app.trading.autopilot import _STATE_KEY
    
    settings = get_settings()
    
    # 1. Check if autopilot is running
    print("\n[1] Autopilot State...")
    try:
        state_dict = storage.kv_get(_STATE_KEY)
        if state_dict:
            from app.trading.autopilot import AutopilotState
            state = AutopilotState.from_dict(state_dict)
            print(f"✓ Autopilot running: {state.running}")
            print(f"  Mode: {state.mode}")
            print(f"  Trades executed: {state.trades_executed}")
            print(f"  Last tick: {state.last_tick_at}")
            print(f"  Last action: {state.last_action}")
            print(f"  Last error: {state.last_error}")
        else:
            print("⚠ Autopilot never started (no state in DB)")
    except Exception as e:
        logger.exception("Autopilot state check failed")
        print(f"✗ Error: {e}")
    
    # 2. Check open positions
    print("\n[2] Open Positions...")
    try:
        positions = storage.kv_get("portfolio_positions") or {}
        print(f"✓ Open positions: {len(positions)}")
        for symbol, pos in list(positions.items())[:3]:
            print(f"  - {symbol}: qty={pos.get('quantity')}, entry={pos.get('entry_price')}")
    except Exception as e:
        logger.exception("Position check failed")
        print(f"✗ Error: {e}")
    
    # 3. Check skip reasons (why trades weren't placed)
    print("\n[3] Skip Reasons (Last 10 Skipped Signals)...")
    try:
        skip_stats = storage.kv_get("autopilot_skip_stats") or {}
        if skip_stats:
            # Top 10 skip reasons
            sorted_reasons = sorted(
                skip_stats.items(), 
                key=lambda x: x[1], 
                reverse=True
            )[:10]
            for reason, count in sorted_reasons:
                print(f"  - {reason}: {count} times")
        else:
            print("⚠ No skip stats recorded")
    except Exception as e:
        logger.exception("Skip stats check failed")
        print(f"✗ Error: {e}")
    
    # 4. Check recent order logs
    print("\n[4] Recent Orders/Skips...")
    try:
        last_debug = storage.kv_get("autopilot_last_tick_debug")
        if last_debug:
            print(f"✓ Last tick debug info:")
            print(f"  Symbol: {last_debug.get('symbol')}")
            print(f"  Timeframe: {last_debug.get('timeframe')}")
            print(f"  Timestamp: {last_debug.get('timestamp')}")
            print(f"  Signal: {last_debug.get('signal')}")
            print(f"  Confidence: {last_debug.get('confidence')}")
            print(f"  Skip reason: {last_debug.get('skip_reason')}")
            print(f"  Executed: {last_debug.get('executed')}")
        else:
            print("⚠ No recent tick debug info")
    except Exception as e:
        logger.exception("Debug info check failed")
        print(f"✗ Error: {e}")
    
    # 5. Simulation: run one tick
    print("\n[5] Autopilot Status...")
    try:
        from app.trading.autopilot import Autopilot
        
        autopilot = Autopilot()
        print(f"✓ Autopilot created")
        print(f"  Current mode: {autopilot.state.mode}")
        print(f"  Running: {autopilot.state.running}")
        
    except Exception as e:
        logger.exception("Autopilot check failed")
        print(f"✗ Error: {e}")
    
    print("\n" + "="*80)
    print("TRACE COMPLETE")
    print("="*80 + "\n")
    
    # Summary
    print("\nKEY FINDINGS:")
    print("  1. Check if DRY_RUN is True (blocks all orders)")
    print("  2. Check if PAPER_TRADING is True (uses paper_exchange, not live)")
    print("  3. Look at Skip Reasons — they tell us why signals were rejected")
    print("  4. If 'Autopilot never started', the scheduler isn't running or crashed")
    print("  5. Check last_error in AutopilotState for recent exceptions")

if __name__ == '__main__':
    asyncio.run(main())
