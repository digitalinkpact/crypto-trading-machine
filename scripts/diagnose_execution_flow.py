#!/usr/bin/env python3
"""
Deep diagnostic: trace execution flow from WebSocket ticks to orders.
Prints exact skip reasons and state at each step.
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
    print("DIAGNOSTIC: Trading Execution Flow")
    print("="*80)
    
    # 1. Check environment & settings
    print("\n[1] Checking environment & Settings...")
    try:
        from app.config import SYMBOLS, TIMEFRAMES, get_settings
        settings = get_settings()
        print(f"✓ DRY_RUN: {settings.dry_run}")
        print(f"✓ PAPER_TRADING: {settings.paper_trading}")
        print(f"✓ SYMBOLS: {len(SYMBOLS)} loaded")
        print(f"✓ TIMEFRAMES: {TIMEFRAMES}")
        print(f"✓ LLM_PROVIDER: {settings.llm_provider}")
    except Exception as e:
        logger.exception("Settings check failed")
        print(f"✗ Settings load failed: {e}")
        return
    
    # Store for later use
    test_symbol = SYMBOLS[0]
    test_timeframe = TIMEFRAMES[0]
    
    # 2. Check DB
    print("\n[2] Checking storage DB...")
    try:
        from app.storage import Storage
        db = Storage()
        print(f"✓ DB path: {db._path}")
        print(f"✓ DB exists: {db._path.exists()}")
    except Exception as e:
        logger.exception("DB check failed")
        print(f"✗ DB check failed: {e}")
    
    # 3. Test exchange client
    print("\n[3] Testing exchange client...")
    try:
        from app.exchange import BinanceUSClient
        client = BinanceUSClient()
        print(f"✓ Exchange client created")
        print(f"⚠ API credentials not set (expected in dev env)")
    except Exception as e:
        logger.exception("Exchange client failed")
        print(f"✗ Exchange client failed: {e}")
        return
    
    # 4. Test WebSocket stream startup
    print("\n[4] Testing WebSocket stream...")
    try:
        from app.exchange.ws_stream import WebSocketStream
        ws = WebSocketStream()
        print(f"✓ WebSocket stream ready")
        print(f"✓ Symbols subscribed: {len(ws.symbols)} (from config)")
    except Exception as e:
        print(f"✗ WebSocket setup failed: {e}")
    
    # 5. Test regime classifier
    print("\n[5] Testing regime classifier...")
    try:
        from app.regime.classifier import RegimeClassifier
        classifier = RegimeClassifier()
        print(f"✓ Regime classifier loaded")
        print(f"✓ Model path: {classifier.model_path}")
    except Exception as e:
        print(f"✗ Regime classifier failed: {e}")
    
    # 6. Test TA pipeline
    print("\n[6] Testing TA indicators...")
    try:
        import numpy as np
        import pandas as pd
        from app.ta.indicators import IndicatorPipeline
        
        pipeline = IndicatorPipeline()
        # Fake candle
        closes = np.array([100, 101, 102, 103, 104])
        result = pipeline.compute(closes, period=5)
        print(f"✓ TA pipeline working")
        print(f"✓ RSI calculated: {result.get('rsi', 'N/A')}")
    except Exception as e:
        print(f"✗ TA pipeline failed: {e}")
    
    # 7. Test autopilot initialization
    print("\n[7] Testing autopilot...")
    try:
        from app.trading.autopilot import Autopilot
        autopilot = Autopilot(client=client)
        print(f"✓ Autopilot created")
        print(f"✓ Paper mode: {autopilot.paper_trading}")
        print(f"✓ Dry run: {autopilot.dry_run}")
        print(f"✓ Agents: {len(autopilot.agents)}")
        for agent in autopilot.agents:
            print(f"  - {agent.__class__.__name__}")
    except Exception as e:
        print(f"✗ Autopilot failed: {e}")
        return
    
    # 8. Simulate one tick cycle
    print("\n[8] Simulating one tick cycle...")
    try:
        from app.trading.portfolio import Portfolio
        
        # Use the test data from section [1]
        test_candle = {
            'symbol': test_symbol,
            'timeframe': test_timeframe.value,
            'timestamp': 1234567890,
            'open': 100.0,
            'high': 105.0,
            'low': 95.0,
            'close': 103.0,
            'volume': 1000.0
        }
        
        print(f"  Test tick: {test_symbol} {test_timeframe.value} @ 103.0")
        
        # Run tick handler
        result = await autopilot.on_candle(test_candle)
        print(f"  ✓ Tick processed")
        print(f"  → Decision: {result}")
        
    except Exception as e:
        logger.exception("Tick cycle failed")
        print(f"  ✗ Tick cycle error: {e}")
    
    print("\n" + "="*80)
    print("DIAGNOSTIC COMPLETE")
    print("="*80 + "\n")

if __name__ == '__main__':
    asyncio.run(main())
