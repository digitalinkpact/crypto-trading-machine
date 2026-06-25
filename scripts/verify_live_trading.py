#!/usr/bin/env python3
"""
Pre-flight verification for live trading setup.
Checks credentials, settings, and risk configuration before going live.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

def verify_live_trading():
    print("\n" + "="*80)
    print("LIVE TRADING PRE-FLIGHT VERIFICATION")
    print("="*80)
    
    # 1. Check .env file
    print("\n[1] Environment Configuration...")
    env_path = Path(".env")
    if not env_path.exists():
        print("✗ .env file not found — create from .env.example first")
        return False
    print(f"✓ .env file exists")
    
    # 2. Load settings
    print("\n[2] Loading Settings...")
    try:
        from app.config import get_settings
        settings = get_settings()
        print("✓ Configuration loaded")
    except Exception as e:
        print(f"✗ Settings load failed: {e}")
        return False
    
    # 3. Verify trading mode
    print("\n[3] Trading Mode...")
    print(f"  LIVE_MODE: {settings.live_mode}")
    print(f"  DRY_RUN: {settings.dry_run}")
    print(f"  PAPER_TRADING: {settings.paper_trading}")
    
    if not settings.live_mode or settings.dry_run or settings.paper_trading:
        print("✗ NOT in live mode — update .env:")
        print("   LIVE_MODE=true")
        print("   DRY_RUN=false")
        print("   PAPER_TRADING=false")
        return False
    print("✓ LIVE MODE ENABLED")
    
    # 4. Verify API credentials
    print("\n[4] Binance.US API Credentials...")
    api_key = settings.binance_api_key.get_secret_value() if settings.binance_api_key else ""
    api_secret = settings.binance_api_secret.get_secret_value() if settings.binance_api_secret else ""
    
    if not api_key or not api_secret:
        print("✗ BINANCE_API_KEY or BINANCE_API_SECRET not set in .env")
        print("  Create at: https://www.binance.us/account/api-management")
        print("  Ensure IP whitelist is enabled")
        return False
    
    if api_key.startswith("your_") or api_secret.startswith("your_"):
        print("✗ API credentials are PLACEHOLDERS in .env")
        return False
    
    print(f"✓ API credentials configured")
    print(f"  Key: {api_key[:10]}...{api_key[-4:]}")
    
    # 5. Verify risk settings
    print("\n[5] Risk Configuration...")
    print(f"  Min confidence: {settings.min_signal_confidence}")
    print(f"  Max position size: {settings.max_position_pct * 100:.1f}% of equity")
    print(f"  Max portfolio risk: {settings.max_portfolio_risk_pct * 100:.1f}% of equity")
    print(f"  Max open positions: {settings.max_open_positions}")
    print(f"  Stop loss: {settings.stop_loss_pct * 100:.2f}%")
    print(f"  Take profit: {settings.take_profit_pct * 100:.2f}%")
    
    if settings.min_signal_confidence < 0.50:
        print("⚠ WARNING: Very low confidence threshold (< 0.50) — may over-trade")
    if settings.max_position_pct > 0.20:
        print("⚠ WARNING: Large max position size (> 20%) — high risk per trade")
    if settings.max_open_positions > 10:
        print("⚠ WARNING: Many concurrent positions allowed (> 10) — correlate risk")
    
    print("✓ Risk settings reviewed")
    
    # 6. Test exchange connection
    print("\n[6] Testing Binance.US Connection...")
    try:
        import asyncio
        from app.exchange import BinanceUSClient
        
        async def test_connection():
            client = BinanceUSClient()
            try:
                # Test public endpoint (no auth)
                price = await client.ticker_price("BTCUSDT")
                print(f"✓ Binance.US reachable: BTCUSDT = ${price}")
                return True
            except Exception as e:
                print(f"✗ Connection test failed: {e}")
                if "401" in str(e) or "Signature" in str(e):
                    print("  → Check API credentials (key/secret mismatch)")
                return False
        
        result = asyncio.run(test_connection())
        if not result:
            return False
    except Exception as e:
        print(f"✗ Connection test error: {e}")
        return False
    
    # 7. Verify database
    print("\n[7] Database Status...")
    try:
        from app.storage import storage
        print(f"✓ Database ready: {storage._path}")
        balances = storage.paper_balances()
        print(f"  Paper balances: {len(balances)} assets (will not be used in live mode)")
    except Exception as e:
        print(f"✗ Database check failed: {e}")
        return False
    
    # 8. Summary
    print("\n" + "="*80)
    print("✓ ALL CHECKS PASSED — READY FOR LIVE TRADING")
    print("="*80)
    print("\nNEXT STEPS:")
    print("  1. CRITICAL: Verify Binance.US API key has IP whitelist enabled")
    print("  2. Start the bot: uvicorn app.main:app --reload")
    print("  3. Monitor first 24 hours: tail -f app.log")
    print("  4. Check dashboard: http://localhost:8000")
    print("  5. Set up alerts: consider Slack/email for trade notifications")
    print("\nRISK REMINDER:")
    print("  ⚠ Real money trades execute immediately")
    print("  ⚠ Slippage & fees differ from paper backtests")
    print("  ⚠ Market gaps can exceed stop-loss levels")
    print("  ⚠ Keep 24/7 monitoring during first week")
    print("\n")
    return True

if __name__ == "__main__":
    success = verify_live_trading()
    sys.exit(0 if success else 1)
