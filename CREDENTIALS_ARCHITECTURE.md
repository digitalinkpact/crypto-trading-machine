# App Files: API Credentials & Live Trading Flow

## 🔑 Credentials Management

### Files Involved

```
app/credentials.py          ← Atomic .env writing (saves API keys)
app/config.py              ← Pydantic Settings (loads from .env)
app/exchange/client.py     ← Uses credentials to auth Binance requests
app/api/routes.py          ← Web dashboard (input form + save endpoint)
.env                       ← Gitignored, holds actual keys
.env.example              ← Template with placeholders
```

### Flow: User → Dashboard → .env → Binance

```
1. User opens http://localhost:8000/settings
2. User enters API key + secret in form
3. User clicks "Save credentials"
   ↓
4. POST /settings/credentials triggered
   ↓
5. app/api/routes.py::save_credentials() called
   ↓
6. app/credentials.py::save_binance_credentials(key, secret) called
   ↓
7. Credentials written to .env atomically:
   - Create temp file with credentials
   - Set 0o600 permissions (owner read/write only)
   - Atomic rename (guarantees no partial writes)
   - Call get_settings.cache_clear() to reload config
   ↓
8. App reloads config from .env
   ↓
9. Next Binance call uses new credentials
```

---

## 📁 Key Files Explained

### 1. `app/config.py` (Single Source of Truth)

```python
class Settings(BaseSettings):
    # Environment variables loaded from .env
    binance_api_key: str = ""  # BINANCE_API_KEY from .env
    binance_api_secret: str = "" # BINANCE_API_SECRET from .env
    live_mode: bool = True      # LIVE_MODE from .env
    dry_run: bool = False
    paper_trading: bool = False
    min_signal_confidence: float = 0.55
    # ... 100+ other settings
    
    @model_validator(mode='after')
    def _apply_live_mode_override(self):
        if self.live_mode:
            self.dry_run = False
            self.paper_trading = False
        return self
```

**Responsibility:** Load configuration from environment, validate constraints

---

### 2. `app/credentials.py` (Atomic .env Writing)

```python
def save_binance_credentials(api_key: str, api_secret: str):
    """Atomically write credentials to .env"""
    _write_env({
        "BINANCE_API_KEY": api_key,
        "BINANCE_API_SECRET": api_secret,
    })
    get_settings.cache_clear()  # Force config reload

def _write_env(updates: dict, drop: tuple = ()):
    """Atomic write with secure permissions"""
    # 1. Read existing .env
    # 2. Create temp file
    # 3. Write updated content
    # 4. Set 0o600 permissions
    # 5. Atomic rename (os.rename is atomic on Linux)
    # 6. Never leaves partial state
```

**Responsibility:** Safe credential persistence to .env

---

### 3. `app/exchange/client.py` (Uses Credentials)

```python
class BinanceUSClient:
    def __init__(self, settings: Settings):
        self.api_key = settings.binance_api_key
        self.api_secret = settings.binance_api_secret
        # Create HTTP client with auth headers
        
    async def place_order(...):
        # Uses self.api_key + self.api_secret to sign requests
        # Sends to https://api.binance.us/
```

**Responsibility:** Authenticate all Binance API calls

---

### 4. `app/api/routes.py` (Dashboard Form)

```python
@router.get("/settings")
async def settings_page():
    # Show HTML form with:
    # - <input type='text' name='api_key'>
    # - <input type='password' name='api_secret'>
    # - <button> Save credentials </button>
    # POST to /settings/credentials

@router.post("/settings/credentials")
async def save_credentials(api_key: str = Form(...), api_secret: str = Form(...)):
    save_binance_credentials(api_key, api_secret)
    return RedirectResponse(url="/settings?saved=1")
```

**Responsibility:** User interface for entering/updating credentials

---

### 5. `.env` (Gitignored Secret Store)

```ini
# DO NOT COMMIT THIS FILE
BINANCE_API_KEY=jKa9WzQp2X3bDfG7hJkLmNoPqRsTuVwXyZ
BINANCE_API_SECRET=mL9pQ2rS5tU6vW7xY8zAbCdEfGhIjKlMnOpQrStUvWxYz
LIVE_MODE=true
DRY_RUN=false
PAPER_TRADING=false
```

**Responsibility:** Secret storage (permission 0o600)

---

## 🔄 Trading Flow with Credentials

```
Startup:
  1. app/config.py loads BINANCE_API_KEY from .env via pydantic
  2. app/exchange/client.py initialized with credentials
  3. Test connection to Binance.US (verify key is valid)

Each Tick (every 15 minutes):
  1. Autopilot runs app/trading/autopilot.py::tick()
  2. Agents generate signals
  3. Decision gates filter signals
  4. For each BUY/SELL:
     - app/exchange/client.py::place_order() called
     - Uses api_key + api_secret to sign request
     - Sends to Binance.US
     - Response stored in SQLite

Credential Update (via Dashboard):
  1. User enters new key/secret on /settings
  2. app/api/routes.py::save_credentials() called
  3. app/credentials.py::save_binance_credentials() called
  4. .env updated atomically
  5. get_settings.cache_clear() forces reload
  6. Next tick uses new credentials
```

---

## ✅ Verification: Are Credentials Wired Correctly?

### Check 1: Config Loads from .env

```bash
python -c "
from app.config import get_settings
s = get_settings()
print(f'API Key exists: {bool(s.binance_api_key)}')
print(f'API Key length: {len(s.binance_api_key) if s.binance_api_key else 0}')
print(f'Live Mode: {s.live_mode}')
"
```

Expected:
```
API Key exists: False       ← No credentials yet (empty string)
API Key length: 0
Live Mode: True            ← Ready for live trading
```

### Check 2: Dashboard Form Works

```bash
# Start bot
uvicorn app.main:app --reload

# In another terminal, test the form
curl -X POST http://localhost:8000/settings/credentials \
  -d "api_key=test_key_12345" \
  -d "api_secret=test_secret_67890" \
  -L  # Follow redirect
```

Expected:
- 303 redirect to /settings?saved=1
- .env file updated
- Next reload shows API key

### Check 3: Credentials Used in Trading

```bash
tail -f app.log | grep -E "BinanceUS|place_order|signed"
```

Look for requests being signed with your key.

---

## 🚨 Security Checklist

- [ ] `.env` is in `.gitignore`
- [ ] Credentials never printed in logs
- [ ] `.env` file permissions are 0o600 (owner read/write only)
- [ ] API key has IP whitelist enabled
- [ ] API key does not have withdraw permissions
- [ ] API key does not have margin/futures permissions
- [ ] Dashboard form uses `type='password'` for secret
- [ ] Dashboard form uses `autocomplete='off'`
- [ ] Credentials never sent in URLs (always POST body)

---

## 📝 Summary

| Component | File | Purpose |
|-----------|------|---------|
| **Loading** | `app/config.py` | Load from .env via pydantic |
| **Saving** | `app/credentials.py` | Atomic .env write with 0o600 |
| **Using** | `app/exchange/client.py` | Sign Binance requests |
| **Input** | `app/api/routes.py` | Web dashboard form |
| **Storage** | `.env` | Gitignored secret store |

**The app is designed so you never manually edit .env. Use the dashboard.**

---

## 🎯 Next Steps

1. Start bot: `uvicorn app.main:app --reload`
2. Open http://localhost:8000/settings
3. Click "Binance.US API Credentials" section
4. Enter your key and secret
5. Click "Save credentials"
6. App handles the rest ✓

**Your credentials are now safely persisted and live trading is ready!**
