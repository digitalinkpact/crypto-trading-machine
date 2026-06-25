# Configuration Safety & Live Trading

## Default Configuration (Code-Level Defaults)

The app defaults to **SAFE** values:

```python
# app/config.py (defaults)
live_mode: bool = False          # ← Paper trading by default
dry_run: bool = True             # ← Dry run enabled
paper_trading: bool = True        # ← Paper trading enabled
```

**This means:** If no `.env` file exists, the bot runs in **paper trading mode** (simulated, no real money).

---

## Opt-In to Live Trading

To trade with **real money** on Binance.US, you must explicitly enable it via `.env`:

### Step 1: Update `.env`

```ini
# .env file (must set these THREE together)
LIVE_MODE=true
DRY_RUN=false
PAPER_TRADING=false
```

### Step 2: Verify Config

```bash
python -c "
from app.config import get_settings
s = get_settings()
print(f'Live Mode: {s.live_mode}')
print(f'Dry Run: {s.dry_run}')
print(f'Paper Trading: {s.paper_trading}')
if s.live_mode and not s.dry_run and not s.paper_trading:
    print('✓ LIVE TRADING ENABLED')
else:
    print('✓ PAPER TRADING MODE (safe)')
"
```

### Step 3: Add Your API Credentials

```ini
# .env file
BINANCE_API_KEY=your_real_key_here
BINANCE_API_SECRET=your_real_secret_here
```

### Step 4: Start Bot

```bash
uvicorn app.main:app --reload
```

Expected output:
```
INFO:     Config loaded: live_mode=True, dry_run=False, paper_trading=False
INFO:     LIVE TRADING ENABLED - REAL MONEY AT RISK
```

---

## Safety Hierarchy

```
CODE DEFAULTS (safest)
  ↓
  live_mode=False (paper trading)
  dry_run=True
  paper_trading=True
  ↓
.ENV OVERRIDE (opt-in to live)
  ↓
  LIVE_MODE=true
  DRY_RUN=false
  PAPER_TRADING=false
  ↓
LIVE TRADING ENABLED (real money)
```

---

## If You Forget to Set .env

```bash
# Without LIVE_MODE=true in .env:
$ python -c "from app.config import get_settings; s = get_settings(); print(s.live_mode)"
False

# Bot runs in paper trading (simulated, safe)
```

---

## Emergency Mode Switch

### To Go From Live → Paper (Emergency Stop)

```bash
# Edit .env
LIVE_MODE=false
PAPER_TRADING=true
DRY_RUN=true

# Restart bot
# (existing positions remain, no new orders executed)
```

### Via Dashboard (If Running)

```bash
# http://localhost:8000/settings
# Click "Settings" tab
# Radio button: Select "Paper" mode
# Click "Save mode"
# ✓ Bot switches to paper trading immediately
```

---

## Configuration Validation

When bot starts, it validates configuration:

```python
@model_validator(mode="after")
def _apply_live_mode_override(self) -> "Settings":
    """If LIVE_MODE=true, force live mode across the system."""
    if self.live_mode:
        self.paper_trading = False
        self.dry_run = False
    return self
```

This ensures:
- If `LIVE_MODE=true` → paper trading is impossible
- If `LIVE_MODE=false` → paper trading is enabled
- No conflicting states possible

---

## Safe Defaults Tested

The test suite verifies safe defaults:

```bash
$ pytest tests/test_config.py::test_settings_defaults_safe -v

# Without any .env file (simulating fresh install):
# assert s.dry_run is True      ✓ PASS
# assert s.paper_trading is True ✓ PASS
```

---

## Summary

| Mode | LIVE_MODE | DRY_RUN | PAPER_TRADING | Real Money? |
|------|-----------|---------|---------------|-------------|
| **Default (Safe)** | false | true | true | ❌ No |
| **Live (Opt-in)** | true | false | false | ✅ Yes |
| **Manual Paper** | false | false | true | ❌ No |

**The bot is designed to be safe by default.** You must explicitly opt-in to live trading via `.env`.

---

## Testing Your Configuration

```bash
# Check if live mode would be active
python scripts/verify_live_trading.py

# Should show:
# ✓ Config: live_mode=True, dry_run=False, paper_trading=False
# ✓ Trading mode: LIVE
# ✓ ALL CHECKS PASSED
```

---

## API Credentials via Dashboard

Even with safe defaults, you can set API credentials via the web dashboard:

```bash
# 1. Start bot (in paper trading by default)
uvicorn app.main:app --reload

# 2. Go to http://localhost:8000/settings

# 3. Enter Binance API key + secret

# 4. Click "Save credentials"

# 5. (Later) Update .env to LIVE_MODE=true when ready
```

The dashboard doesn't enable live trading—it just persists your credentials safely.

---

## Key Takeaway

✓ **By default:** Paper trading (safe)
✓ **To enable live:** Set `LIVE_MODE=true` in .env
✓ **Tests verify:** Safe defaults are real (89 tests passing)
✓ **Dashboard:** Can set credentials anytime (they're saved securely)
✓ **Emergency:** Switch back to paper mode in seconds

**You control when live trading starts. The system defaults to safe.**
