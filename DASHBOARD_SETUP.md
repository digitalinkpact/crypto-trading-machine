# Set API Keys Through Dashboard (Recommended)

The app has a secure settings page for entering Binance.US API credentials without manual .env editing.

## Quick Setup

### 1. Start the Bot

```bash
cd /workspaces/crypto-trading-machine
uvicorn app.main:app --reload
```

Expected output:
```
INFO:     Uvicorn running on http://127.0.0.1:8000
```

### 2. Open Dashboard

Open in browser: **http://localhost:8000**

You should see:
- Portfolio overview (empty until trades start)
- Navigation: Dashboard | Trades | Settings | Audit | Account

### 3. Click "Settings" Tab

You'll see 3 sections:

#### Section 1: Trading Mode
- **Paper** (simulated, recommended for first test)
- **Live** (real Binance.US orders, real money)

Currently set to: **Live** (from config.py update)

#### Section 2: Risk Gates
Adjust trading parameters:
- Stop-loss: 5% (force-exit if down 5%)
- Take-profit: 15% (force-exit if up 15%)
- Min confidence: 0.55 (signal threshold)
- Max positions: 5 (concurrent open trades)

#### Section 3: Binance.US API Credentials ⬅️ **THIS IS WHERE YOU SET KEYS**

### 4. Enter Your Binance.US API Key & Secret

**Step 1:** Go to https://www.binance.us/account/api-management

**Step 2:** Create API key with these permissions:
- ✓ Spot & Margin Trading
- ✓ Reading enabled
- ✗ NO withdrawals
- IP Whitelist: Your server/machine IP (CRITICAL)

**Step 3:** Copy Key and Secret

**Step 4:** Paste into the Dashboard form:

```
API key:     [paste your key here]
API secret:  [paste your secret here]
```

**Step 5:** Click "Save credentials"

Expected result:
```
✓ Green banner: "API keys saved."
Status changes to: "API keys are set."
```

---

## Verify It Works

After saving credentials:

```bash
# Test connection
curl http://localhost:8000/

# Should show portfolio dashboard
# If connected to Binance, you'll see real-time market data loading
```

Or in terminal:
```bash
python scripts/verify_live_trading.py
```

Should now show:
```
✓ API credentials configured
✓ Binance.US reachable: BTCUSDT = $...
✓ ALL CHECKS PASSED
```

---

## Security Notes

✅ **Secure:**
- Credentials stored in `.env` (gitignored, not in git)
- Written with 0o600 permissions (only your user can read)
- Dashboard form uses `autocomplete='off'` + password field for secret
- Keys used only for Binance orders, never logged

❌ **NOT Secure:**
- Credentials in git history
- API key without IP whitelist
- API key with withdrawal permission

---

## Important: IP Whitelist on Binance

On Binance.US API Settings, enable **IP Whitelist** and add:
- If testing locally: Your machine's public IP (find via `curl ifconfig.co`)
- If testing on server: That server's public IP
- If testing on dev container: Consider a wide CIDR like `/24` if multiple VMs

**This is the single most important security step.**

---

## Commands Available on Dashboard

| Action | How |
|--------|-----|
| Start trading | Click "Start autopilot" button |
| Stop trading | Click "Stop autopilot" button |
| Change trading mode | Paper ↔ Live radio button |
| Adjust risk settings | Edit fields + "Save risk settings" |
| Update API credentials | Enter new key/secret + "Save credentials" |
| View P&L | "Trades" tab shows all fills + profit/loss |
| View audit log | "Audit" tab shows all trades timestamped |

---

## File Saved

After you save credentials on the dashboard:
```bash
cat .env | grep BINANCE
# Should show:
# BINANCE_API_KEY=jKa9WzQp2X3bDfG7hJ...
# BINANCE_API_SECRET=mL9pQ2rS5tU6vW7xY8z...
```

The file is created automatically with secure permissions (0o600).

---

## If Something Goes Wrong

### "API keys saved" but verification fails

1. Check Binance IP whitelist:
   https://www.binance.us/account/api-management → Settings → IP Whitelist

2. Verify key/secret copied correctly (no trailing spaces)

3. Check if Binance is reachable:
   ```bash
   curl https://api.binance.us/api/v3/ping
   # Should return: {}
   ```

### Dashboard won't load

```bash
# Check if bot is running
ps aux | grep uvicorn

# Restart if needed
uvicorn app.main:app --reload
```

### Previous .env had wrong keys

Edit and remove old values:
```bash
nano .env
# Delete old BINANCE_API_KEY and BINANCE_API_SECRET lines
# Save (Ctrl+X, Y, Enter)

# Then save new keys via dashboard
```

---

## Next Steps

1. ✓ Start bot: `uvicorn app.main:app --reload`
2. ✓ Open http://localhost:8000
3. ✓ Go to Settings tab
4. ✓ Enter your Binance API credentials
5. ✓ Click "Save credentials"
6. ✓ Verify: `python scripts/verify_live_trading.py`
7. ✓ Start autopilot: Click "Start autopilot" button
8. ✓ Monitor: Watch logs and trades tab

**You're now ready for live trading!**
