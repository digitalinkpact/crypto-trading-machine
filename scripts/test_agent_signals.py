#!/usr/bin/env python3
"""
Test signal generation to understand if agents are producing BUY vs SELL signals.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from decimal import Decimal

async def main():
    from app.agents import TrendFollowerAgent, MeanReversionAgent, BreakoutAgent
    from app.agents.base import AgentContext
    from app.config import Timeframe
    from app.ta import add_indicators
    
    print("\n" + "="*80)
    print("AGENT SIGNAL TEST")
    print("="*80 + "\n")
    
    # Create a synthetic bullish candle dataset
    print("[1] Creating synthetic bullish market data...")
    dates = pd.date_range(end=datetime.now(timezone.utc), periods=300, freq='1h')
    
    # Strong uptrend: price climbs steadily
    prices = np.linspace(100, 130, 300) + np.random.normal(0, 0.5, 300)
    
    df = pd.DataFrame({
        'open': prices + np.random.uniform(-0.5, 0.5, 300),
        'high': prices + np.random.uniform(0.5, 2.0, 300),
        'low': prices - np.random.uniform(0.5, 2.0, 300),
        'close': prices,
        'volume': np.random.uniform(1000, 10000, 300),
        'quote_volume': prices * np.random.uniform(1000, 10000, 300),
        'trades': np.random.randint(50, 500, 300),
        'open_time': dates,
    }, index=pd.Index(dates, name='close_time'))
    
    # Add indicators
    try:
        df = add_indicators(df)
        print(f"✓ Added indicators")
        print(f"  RSI: {df['rsi_14'].iloc[-1]:.1f}")
        print(f"  EMA20: {df['ema_20'].iloc[-1]:.2f}")
        print(f"  EMA50: {df['ema_50'].iloc[-1]:.2f}")
        print(f"  EMA200: {df['ema_200'].iloc[-1]:.2f}")
        print(f"  Close: {df['close'].iloc[-1]:.2f}")
    except Exception as e:
        print(f"✗ Indicator addition failed: {e}")
        return
    
    # Create context
    ctx = AgentContext(
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        df=df,
        regime="uptrend"
    )
    
    # Test agents
    print("\n[2] Testing agents on bullish data...")
    agents = [
        TrendFollowerAgent(),
        MeanReversionAgent(),
        BreakoutAgent(),
    ]
    
    for agent in agents:
        try:
            sig = agent.analyze(ctx)
            if sig:
                print(f"✓ {agent.name:20} → {sig.action.value:4} conf={sig.confidence:.2f}")
            else:
                print(f"⊘ {agent.name:20} → No signal")
        except Exception as e:
            print(f"✗ {agent.name:20} → Error: {e}")
    
    # Now create bearish data
    print("\n[3] Testing agents on bearish data...")
    prices_bear = np.linspace(130, 100, 300) + np.random.normal(0, 0.5, 300)
    df_bear = pd.DataFrame({
        'open': prices_bear + np.random.uniform(-0.5, 0.5, 300),
        'high': prices_bear + np.random.uniform(0.5, 2.0, 300),
        'low': prices_bear - np.random.uniform(0.5, 2.0, 300),
        'close': prices_bear,
        'volume': np.random.uniform(1000, 10000, 300),
        'quote_volume': prices_bear * np.random.uniform(1000, 10000, 300),
        'trades': np.random.randint(50, 500, 300),
        'open_time': dates,
    }, index=pd.Index(dates, name='close_time'))
    
    try:
        df_bear = add_indicators(df_bear)
        print(f"✓ Added indicators")
        print(f"  RSI: {df_bear['rsi_14'].iloc[-1]:.1f}")
        print(f"  EMA20: {df_bear['ema_20'].iloc[-1]:.2f}")
        print(f"  EMA50: {df_bear['ema_50'].iloc[-1]:.2f}")
        print(f"  EMA200: {df_bear['ema_200'].iloc[-1]:.2f}")
        print(f"  Close: {df_bear['close'].iloc[-1]:.2f}")
    except Exception as e:
        print(f"✗ Indicator addition failed: {e}")
        return
    
    ctx_bear = AgentContext(
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        df=df_bear,
        regime="downtrend"
    )
    
    for agent in agents:
        try:
            sig = agent.analyze(ctx_bear)
            if sig:
                print(f"✓ {agent.name:20} → {sig.action.value:4} conf={sig.confidence:.2f}")
            else:
                print(f"⊘ {agent.name:20} → No signal")
        except Exception as e:
            print(f"✗ {agent.name:20} → Error: {e}")
    
    print("\n" + "="*80)
    print("If you see mostly SELL signals and few BUY signals in bearish data,")
    print("that explains why trades aren't executing — the market is in a downtrend")
    print("and the trend filter is correctly blocking BUYS.")
    print("="*80 + "\n")

if __name__ == '__main__':
    asyncio.run(main())
