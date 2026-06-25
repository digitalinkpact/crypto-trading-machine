# Live Trading Setup Guide

## Status: ✓ Configuration Ready

The bot is now configured for **live trading on Binance.US**:

```
✓ LIVE_MODE=true (enabled)
✓ DRY_RUN=false (disabled)
✓ PAPER_TRADING=false (disabled)
✓ Confidence threshold=0.55 (more aggressive entries)
```

---

## Step 1: Get Binance.US API Credentials

### Create API Key with Proper Restrictions

1. Go to: https://www.binance.us/account/api-management
2. Click **"Create API Key"**
3. Choose **"API Key"** (not Smart Contract Platform)
4. Set **Label**: "Crypto Trading Bot - Live"

### Restrict Permissions (CRITICAL for security)

5. Go to **API Restrictions** (gear icon):
   - ✓ Enable "Spot & Margin Trading" 
   - ✗ DO NOT check "Enable Withdrawals"
   - ✓ Enable "Reading" (required for data)
   - ✗ DO NOT check "API Key Trading"
   - ✗ DO NOT check "API Key Withdrawals"

6. **Enable IP Whitelist**:
   - Enter your server IP (or your machine's public IP if testing locally)
   - This is the **most important security step** — it restricts API key to your machine only

7. **Save** the API key and secret securely

---

## Step 2: Update .env with Your Credentials

Edit `.env` file in the repo root:

```bash
nano .env
```

Replace placeholders:

```ini
# Find these lines:
BINANCE_API_KEY=your_api_key_here
BINANCE_API_SECRET=your_api_secret_here

# Replace with your actual credentials:
BINANCE_API_KEY=jKa9WzQp2X3bDfG7hJ...
BINANCE_API_SECRET=mL9pQ2rS5tU6vW7xY8z...
```

Save the file (Ctrl+X, Y, Enter if using nano).

---

## Step 3: Verify Setup

Run the pre-flight verification:

```bash
python scripts/verify_live_trading.py
```

Expected output:
```
✓ .env file exists
✓ Configuration loaded
✓ LIVE MODE ENABLED
✓ API credentials configured
✓ Binance.US reachable: BTCUSDT = $...
✓ Database ready
✓ ALL CHECKS PASSED — READY FOR LIVE TRADING
```

---

## Step 4: Start the Bot

```bash
# Option A: Development mode (with auto-reload)
uvicorn app.main:app --reload

# Option B: Production mode
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Expected startup sequence:
```
[startup] Loading settings...
[startup] Initializing auth...
[startup] Loading exchange filters...
[startup] Seeding paper account (not used in live mode)...
[startup] Starting WebSocket price stream...
[startup] Starting APScheduler...
[scheduler] Market data job scheduled: every 15 minutes
[scheduler] Autopilot job scheduled: every 15 minutes (:02, :17, :32, :47)
[trading] Autopilot started (live mode)
```

---

## Step 5: Monitor First Trades

### Watch Real-Time Logs

```bash
tail -f app.log | grep -E "BUY|SELL|executed|skip|ERROR"
```

### Check Dashboard

Open browser: http://localhost:8000
- **Portfolio**: Current positions + P&L
- **Equity Curve**: Daily balance progression
- **Agent Stats**: Win-rate per agent
- **Settings**: Adjust risk parameters live

### Check Skip Reasons

If no trades after 1 hour:

```bash
python scripts/trace_trade_execution.py
```

Will show:
- Autopilot running status
- Open positions
- Skip reasons (why signals were rejected)
- Last signal composition

---

## Risk Configuration (Current)

| Setting | Value | Notes |
|---------|-------|-------|
| Min Confidence | 0.55 | Lower = more entries |
| Max Position % | 5% | Per-trade size cap |
| Max Portfolio Risk | 25% | Max simultaneous exposure |
| Max Open Positions | 5 | Concurrent trades |
| Stop Loss | 5% | Hard stop per position |
| Take Profit | 15% | Auto-exit target |
| Trailing Stop | 2% | From high water mark |
| Max Hold | 7 days | Force exit time limit |

### ⚠️ Recommended First Week Settings

Consider starting even more conservative:

```bash
# Edit app/config.py for first week:
min_signal_confidence: 0.65  # Higher than 0.55
max_position_pct: 0.02       # Smaller: 2% vs 5%
max_open_positions: 2        # Fewer concurrent
```

Then increase after 50 trades if profitable.

---

## Expected Behavior

### Tick Execution (Every 15 Min)

1. **:00 — Data refresh**: Fetch 500 OHLCV candles × 25 symbols × 4 timeframes
2. **:02 — Autopilot**: 
   - Check risk gates (stop-loss, take-profit, max-hold)
   - Run 7 agents in parallel
   - Aggregate signals (weighted voting)
   - Execute BUY/SELL through decision tree
3. **:12 — ML Learning** (hourly): Label outcomes, retrain model
4. **:55 — Equity Snapshot**: Record daily balance for P&L curve

### Order Execution

- **BUY**: Positions sized with Kelly fraction + ATR volatility scaling
- **SELL**: Triggered by TA signals OR stop-loss/TP gates
- **Fees**: ~0.1% per side (Binance.US taker fee) deducted from order notional
- **Slippage**: Usually 1-5 bps on mid-cap coins

---

## Troubleshooting

### No Trades After 1+ Hour

Check in order:

```bash
# 1. Is autopilot running?
python scripts/trace_trade_execution.py

# 2. Are ticks happening?
tail -f app.log | grep "autopilot_tick"

# 3. What skip reasons?
python scripts/trace_trade_execution.py  # Shows skip_stats
```

**Common blockers:**
- `low_confidence` → Signals below 0.55 threshold
- `breaker_tripped` → Portfolio down -3% (circuit breaker)
- `already_held` → Position open in that symbol (1 per symbol limit)
- `cooldown` → Bought same symbol < 60 min ago
- `max_positions` → 5 positions already open
- `orderbook_gate` → Spread too wide on Binance.US

### API Key Rejected

```
Error: binance.exceptions.BinanceAPIException: 401: Invalid API-key
```

**Fix:**
1. Verify key/secret copied correctly (no spaces)
2. Check IP whitelist on Binance — is your public IP whitelisted?
3. Confirm "Spot & Margin Trading" enabled in restrictions
4. Try a fresh key if repeated failures

### "Insufficient Balance"

Means bot tried to buy but account has < $10 USDT:

```bash
# Check account on Binance.US directly:
https://www.binance.us/account/wallet/deposit/crypto/USDT
```

---

## Safety Checklist Before First Trade

- [ ] API key IP whitelist enabled (**critical**)
- [ ] Withdrawals **disabled** on API key
- [ ] Trading permissions **enabled**
- [ ] Small account balance initially ($100-500)
- [ ] `.env` file **NOT committed** to git
- [ ] First 24h monitoring active (watch logs)
- [ ] Stop command ready: `curl -X POST http://localhost:8000/stop`
- [ ] Have emergency exit plan (manual Binance.US liquidation)

---

## Emergency Stop

If needed, stop the bot immediately:

```bash
# Via API
curl -X POST http://localhost:8000/stop

# Or via terminal (Ctrl+C)
# Then restart: uvicorn app.main:app
```

---

## Next Steps

1. ✓ Follow **Step 1-2** above to set up Binance credentials
2. ✓ Run verification: `python scripts/verify_live_trading.py`
3. ✓ Start bot: `uvicorn app.main:app --reload`
4. ✓ Monitor first 24h: `tail -f app.log`
5. ✓ After 10 trades, review `/trades` endpoint for P&L

---

## Support

If issues:
1. Check logs: `tail -f app.log | head -50`
2. Run diagnostics: `python scripts/trace_trade_execution.py`
3. Verify API: `python scripts/verify_live_trading.py`
4. Check Binance.US account directly (Web UI)

**Remember: This is real money. Start small, monitor actively, scale gradually.**
