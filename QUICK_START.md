# 🚀 Quick Start: Live Trading Setup

## ✅ Current Status

```
✓ CODE DEFAULTS: Safe (paper trading enabled)
✓ Min Confidence: 0.55 (aggressive entries, tuned for live)
✓ Max Positions: 5
✓ Stop Loss: 5% | Take Profit: 15%
✓ API Credentials: Can be set via dashboard
✓ Live Mode: Disabled by default (must opt-in via .env)
```

**The app defaults to SAFE paper trading. You control when to go live.**

---

## 📋 How to Add Your API Keys

### Option 1: Web Dashboard (Recommended) ⭐

```bash
# Step 1: Start the bot (paper trading by default - safe!)
uvicorn app.main:app --reload

# Step 2: Open browser
http://localhost:8000

# Step 3: Click "Settings" tab
# Step 4: Scroll to "Binance.US API Credentials" section
# Step 5: Paste your API key and secret
# Step 6: Click "Save credentials"
```

The app will:
- Validate your credentials
- Save to `.env` with secure permissions
- Show confirmation: "API keys saved"
- Bot runs in paper trading (safe, simulated)

### Option 2: Manual .env (If Dashboard Not Available)

```bash
nano .env
```

Find and update:
```ini
BINANCE_API_KEY=your_key_here
BINANCE_API_SECRET=your_secret_here
```

---

## 🔐 Before You Proceed

**Get your Binance.US API Key:**

1. Go to: https://www.binance.us/account/api-management
2. Click "Create API Key"
3. Choose "API Key" option
4. Label it: "Crypto Trading Bot - Live"

**Set Permissions (CRITICAL):**
- ✓ Enable "Spot & Margin Trading"
- ✓ Enable "Reading"
- ✗ DO NOT enable "Withdrawals"
- ✓ Enable "IP Whitelist" → Add your IP

**Get your IP:**
```bash
curl ifconfig.co
# Output example: 123.45.67.89
# Add this to Binance IP whitelist
```

---

## 🚀 When Ready: Switch to Live Trading

The bot defaults to **paper trading** (safe, simulated). To trade with real money:

### Edit `.env` File

```ini
# .env (MUST set these three together to enable live trading)
LIVE_MODE=true
DRY_RUN=false
PAPER_TRADING=false

# Also add your credentials if not already done:
BINANCE_API_KEY=your_actual_key_here
BINANCE_API_SECRET=your_actual_secret_here
```

### Restart Bot

```bash
# Kill existing bot (Ctrl+C)
# Then restart:
uvicorn app.main:app --reload
```

Expected output:
```
INFO: Config loaded: live_mode=True, dry_run=False, paper_trading=False
INFO: LIVE TRADING ENABLED - REAL MONEY AT RISK
```

### To Revert to Paper Trading (Emergency)

```ini
# .env (set to safe)
LIVE_MODE=false
PAPER_TRADING=true
```

---

## 🎯 Command Summary

```bash
# 1. Start the bot (paper trading by default)
cd /workspaces/crypto-trading-machine
uvicorn app.main:app --reload

# 2. Open dashboard
# http://localhost:8000

# 3. Set API credentials (via Settings tab or .env)
# App runs safely in paper trading until you opt-in to live

# 4. When ready: Update .env with LIVE_MODE=true (see above)

# 5. Monitor trades
tail -f app.log | grep -E "BUY|SELL|executed"

# 6. Emergency stop
# Dashboard: Click "Stop autopilot"
# Or Terminal: Ctrl+C
```

---

## 📊 Dashboard Features

| Feature | Location | Notes |
|---------|----------|-------|
| **Portfolio** | Dashboard tab | Real-time balance + positions |
| **Trades** | Trades tab | P&L per closed trade |
| **Settings** | Settings tab | API keys, risk params, trading mode |
| **Start/Stop** | Dashboard | Control autopilot |
| **Audit Log** | Audit tab | All trades timestamped |

---

## ⚠️ Before First Trade

- [ ] API key has IP whitelist enabled
- [ ] Withdrawals disabled on API key
- [ ] Trading permissions enabled
- [ ] Small account balance ($100-500)
- [ ] `.env` file NOT in git
- [ ] Monitoring active in first 24h
- [ ] Emergency stop plan ready

---

## 🎬 First Trade Expected In

**~15 minutes** after bot starts:

1. `:02` past hour → Data refresh (fetch candles)
2. `:17` past hour → Autopilot tick (generate signals)
3. If signals pass gates → **BUY order placed**
4. Next ticks → Monitor for SELL signals

Watch logs:
```bash
tail -f app.log | grep -A2 -B2 "executed"
```

---

## ❓ FAQ

**Q: Where are API keys stored?**
A: In `.env` with 0o600 permissions (secure, not readable by other users)

**Q: Can I change keys without restarting?**
A: Yes! Update on Settings tab → "Save credentials"

**Q: What if Binance rejects my order?**
A: Check logs for the reason. Common:
- Insufficient balance
- API key permissions
- IP not whitelisted

**Q: How do I stop trading?**
A: Click "Stop autopilot" on dashboard

**Q: Is my account safe?**
A: API key restricted to:
- Your IP only (IP whitelist)
- Spot trading only (no margin/futures)
- No withdrawals allowed

---

## 📚 Full Documentation

- **Setup Details**: [LIVE_TRADING_SETUP.md](LIVE_TRADING_SETUP.md)
- **Dashboard Guide**: [DASHBOARD_SETUP.md](DASHBOARD_SETUP.md)
- **Architecture**: [ARCHITECTURE_ANALYSIS.md](ARCHITECTURE_ANALYSIS.md)
- **Troubleshooting**: [LIVE_TRADING_SETUP.md#troubleshooting](LIVE_TRADING_SETUP.md#troubleshooting)

---

## 🚀 Ready? 

**Start the bot:**
```bash
uvicorn app.main:app --reload
```

**Then:**
1. Open http://localhost:8000
2. Click Settings
3. Enter your Binance API credentials
4. Click "Save credentials"
5. Click "Start autopilot"
6. Watch first trades execute!

**Remember: Real money. Monitor actively. Start small.**
