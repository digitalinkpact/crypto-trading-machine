# AI Crypto Trading Machine — Complete Architecture Analysis

**Status:** Active system with 7 cooperating agents across 4 timeframes, ML gate, paper/live modes, and on-chain risk guards.

---

## 1. Entry Point: FastAPI Lifespan (`app/main.py`)

### Core Responsibilities
- **Startup sequence**: Initialize logging, load exchange filters, seed paper account, start scheduler
- **Shutdown graceful cleanup**: Stop websocket stream, shutdown scheduler, release resources
- **Lifespan guarding**: LLM provider availability check, fail-loudly if `llm_in_trading_loop=true` but LLM disabled

### Key Function Signatures
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan context manager — runs once at startup/shutdown."""
    # Boot sequence:
    # 1. configure_logging()
    # 2. storage.purge_expired_sessions()
    # 3. filters.load()  # Binance.US exchange info
    # 4. paper_exchange.ensure_seeded()
    # 5. live_prices.start()  # WebSocket cache
    # 6. build_scheduler()  # APScheduler with 5 scheduled jobs
    
    yield  # App runs here
    
    # Shutdown sequence (finally block):
    # 1. live_prices.stop()
    # 2. scheduler.shutdown(wait=False)

app = FastAPI(title="AI Crypto Trading Machine", lifespan=lifespan)
app.middleware("http")(auth_guard)  # Session-based auth
```

### HTTP Endpoints (via `app/api/routes.py`)
- **GET** `/` → Dashboard HTML (portfolio, P&L, agent stats)
- **GET** `/trades` → Closed trades table, agent attribution
- **POST** `/start` → Start autopilot (paper or live mode)
- **POST** `/stop` → Stop autopilot and liquidate positions
- **GET** `/settings` → Settings form
- **POST** `/settings` → Save API keys, risk caps, trading mode
- **Auth routes**: `/auth/login`, `/auth/verify`, `/auth/password`, `/auth/audit`

---

## 2. Scheduler Layer (`app/scheduler/jobs.py`)

### Architecture
- **Single AsyncIOScheduler** shared by FastAPI lifespan
- **5 cron-triggered jobs** orchestrate the trading system

### Job Definitions

```python
def build_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    
    # Job 1: Refresh market data every 15 minutes
    scheduler.add_job(
        refresh_market_data,
        CronTrigger(minute="*/15"),
        id="market_data"
    )
    
    # Job 2: Execute autopilot tick at 4 fixed minutes/hour
    scheduler.add_job(
        autopilot_tick,
        CronTrigger(minute="2,17,32,47"),
        id="autopilot"
    )
    
    # Job 3: LLM-only signal pass (off hot path) every hour @:07
    scheduler.add_job(
        llm_signal_pass,
        CronTrigger(minute="7"),
        id="llm_pass"
    )
    
    # Job 4: ML learning cycle every 6h @:12
    scheduler.add_job(
        ml_learning_pass,
        CronTrigger(minute="12", hour="*/6"),
        id="ml_learning"
    )
    
    # Job 5: Portfolio equity curve snapshot every hour @:55
    scheduler.add_job(
        equity_snapshot,
        CronTrigger(minute="55"),
        id="equity_curve"
    )
    
    return scheduler
```

### Critical Job Functions

#### `refresh_market_data()`
```python
async def refresh_market_data() -> None:
    """
    Fetch OHLCV candles for all (symbol, timeframe) pairs.
    ¡ Blocks autopilot tick if data fetch times out ¡
    """
    repo = OHLCVRepository()
    symbols = await get_symbols()  # Dynamic or static list
    for symbol in symbols:
        for tf in TIMEFRAMES:  # [1h, 4h, 1d, 1w]
            await repo.get(symbol, tf, refresh=True)
            # Persists to pickle cache at: data/cache/{symbol}_{tf}.pkl
```

#### `autopilot_tick()`
```python
async def autopilot_tick() -> None:
    """Called 4 times/hour at :02, :17, :32, :47.
    
    No-op when autopilot.state.running == False.
    Coordinates the entire trading loop (see §3).
    """
    await autopilot.tick()
```

#### `llm_signal_pass()`
```python
async def llm_signal_pass() -> None:
    """
    Run agents with LLM enabled (off the hot path).
    Runs once/hour; stashes aggregated signals in KV store.
    Dashboard reads this to show "what would the LLM do now?"
    
    OFF the default autopilot tick → LLM calls don't block order placement.
    """
    if not reasoner.enabled:
        return
    signals = await run_all_agents(use_llm=True)
    storage.kv_set("llm_signals", payload)
```

#### `ml_learning_pass()`
```python
async def ml_learning_pass() -> None:
    """
    Label matured signal events (horizon_minutes old).
    Retrain signal-quality model every 6 hours.
    
    Flow:
      1. label_matured_signal_events()  → resolve BUY/SELL outcomes
      2. train_signal_quality_model()   → logistic regression on features
      3. Save as signal_quality_v1 in model artifact store
    """
    result = await run_learning_cycle()
```

---

## 3. Trading Loop: The Autopilot Tick (`app/trading/autopilot.py`)

### State Machine
```python
class AutopilotState:
    running: bool = False           # User can START/STOP
    mode: str = "paper" | "live"    # From settings.paper_trading
    started_at: datetime            # When START was clicked
    last_tick_at: datetime          # Last successful tick
    trades_executed: int            # Cumulative for this run
    starting_balance_usdt: Decimal  # Baseline for drawdown breaker
    cooldowns: dict[str, str]       # symbol → iso timestamp (min 1h between buys)
    last_action: str                # Diagnostic message
    last_error: str                 # Error message if last tick failed
```

### Tick Execution Flow

```python
async def Autopilot.tick(self) -> None:
    """
    Called 4×/hour by scheduler. Runs ATOMICALLY with cross-process lock.
    
    ┌─────────────────────────────────────────────────┐
    │ TICK START                                      │
    ├─────────────────────────────────────────────────┤
    │ 0. Guard: Cross-process + in-process lock      │
    │    ├─ TTL=300s (crash safety)                  │
    │    └─ Skip if another instance/process holds it│
    │                                                 │
    │ 1. RISK GATES (BEFORE agents)                  │
    │    ├─ Hard stop-loss:  loss > -5% → FORCE SELL│
    │    ├─ Take-profit:     gain > +15% → FORCE SELL│
    │    ├─ Trailing stop:   drop 2% from HWM → SELL│
    │    └─ Max hold time:   >7 days open → FORCE SELL│
    │                                                 │
    │ 2. CIRCUIT BREAKER CHECK                       │
    │    └─ DD: (current - starting) / starting      │
    │       If < -3% → SKIP ALL NEW BUYS (risk-off) │
    │                                                 │
    │ 3. AGENT SIGNALS (if running)                  │
    │    ├─ run_all_agents(use_llm=llm_in_loop)     │
    │    └─ Returns dict[symbol → Signal]            │
    │                                                 │
    │ 4. EXECUTE SIGNALS                             │
    │    └─ See §4.1 decision tree                   │
    │                                                 │
    │ SAVE STATE (in finally)                        │
    └─────────────────────────────────────────────────┘
    """
    if not self.state.running:
        return
    
    # Cross-process lock
    if not storage.try_acquire_lock("autopilot_tick", owner=self._owner):
        log.info("tick skipped — another process holds lock")
        return
    
    async with self._lock:  # In-process guard
        try:
            # 0. Cold-start guards
            if self.state.mode == "paper":
                paper_exchange.ensure_seeded()
            await filters.load()
            
            # 1. Risk exits
            await self._run_risk_gates()
            
            # 2. Drawdown circuit breaker
            breaker_tripped = await self._check_circuit_breaker()
            
            # 3. Get fresh signals
            signals = await run_all_agents(use_llm=get_settings().llm_in_trading_loop)
            
            # 4. Execute (skip buys if breaker tripped)
            await self._execute(signals, allow_buys=not breaker_tripped)
        finally:
            storage.release_lock("autopilot_tick", owner=self._owner)
```

### Signal Execution Decision Tree (§4.1)

```
FOR EACH symbol (ranked by confidence DESC):
  │
  ├─ [HOLD ACTION] → SKIP
  │
  ├─ [CONFIDENCE GATE] conf < min_signal_confidence
  │  └─ SKIP (log as "low_confidence")
  │
  ├─ [ML QUALITY GATE] (if enabled)
  │  └─ Load signal_quality_v1 model
  │  └─ Compute P(win) via logistic regression
  │  └─ If P(win) < ml_gate_threshold → SKIP (log as "ml_gate")
  │  └─ [Record for learning BEFORE gate decision]
  │
  ├─ [SYMBOL LISTED?] not in Binance.US → SKIP
  │
  ├─ [ACTION == BUY]
  │  ├─ Breaker tripped?        → SKIP ("breaker_tripped")
  │  ├─ Already held?           → SKIP ("already_held", avoid pyramid)
  │  ├─ On cooldown? (1h+)      → SKIP ("cooldown")
  │  ├─ Max positions reached?  → SKIP ("risk_cap")
  │  ├─ Max long exposure cap?  → SKIP ("risk_cap")
  │  ├─ [TREND GATE] close < ema_200? → SKIP ("trend_gate", avoid downtrend longs)
  │  ├─ [FUNDING GATE] futures funding negative? → SKIP (avoid shorts against us)
  │  ├─ [ON-CHAIN GATE] exchange inflow spike? → SKIP (whale selling)
  │  ├─ [ORDERBOOK GATE] spread > max_spread_pct? → SKIP
  │  ├─ [SIZE & NOTIONAL CHECK] per_trade_usdt < $10? → SKIP
  │  └─ ✓ PLACE BUY
  │     └─ Set cooldown[symbol] = now
  │     └─ Increment open_count
  │     └─ Update long_exposure_pct
  │
  ├─ [ACTION == SELL]
  │  ├─ Balance available for this coin?
  │  ├─ Open position exists in this mode?
  │  └─ ✓ PLACE SELL
  │     └─ Clear high-water-mark
  │
  └─ [UPDATE SKIP STATS]
     └─ Persisted for UI telemetry
```

### Critical Decision Points Where Trades Can Be Skipped

| Decision Gate | Skipped Counter | Trigger | Impact |
|---|---|---|---|
| **Confidence** | `low_confidence` | `conf < min_signal_confidence` (default 0.65) | All actions |
| **ML Quality** | `ml_gate` | `P(win) < ml_gate_threshold` (default 0.55) | BUY/SELL only |
| **Breaker** | `breaker_tripped` | Portfolio DD < -3% | BUY only |
| **Already held** | `already_held` | Position already open in mode | BUY only |
| **Cooldown** | `cooldown` | Last BUY < 60 min ago | BUY only |
| **Positions cap** | `risk_cap` | `open_count >= max_open_positions` | BUY only |
| **Exposure cap** | `risk_cap` | `long_exposure_pct >= max_long_exposure_pct` | BUY only |
| **Trend gate** | `trend_gate` | `close < ema_200` (avoid downtrend longs) | BUY only |
| **Funding gate** | `funding_gate` | Perpetuals funding too negative | BUY only |
| **On-chain gate** | `onchain_gate` | Exchange inflow spike | BUY only |
| **Orderbook** | `orderbook_gate` | Spread > max_spread_pct or insufficient depth | BUY only |
| **Notional** | `insufficient_usdt` | `per_trade < $10` and cash < $10 | BUY only |
| **Filter reject** | `filter_reject_{buy\|sell}` | `qty < MIN_NOTIONAL` or `qty < MIN_QTY` | BUY/SELL |
| **Symbol unlisted** | `not_listed` | Symbol not in TRADING status | BUY/SELL |

**Risk gate exits (bypasses all checks above):**
- Hard stop-loss, take-profit, trailing stop, max hold time → **ALWAYS exit** regardless of signals

---

## 4. Signal Generation: The 7 Agents (`app/agents/*`)

### Agent Runner Architecture

```python
async def run_all_agents(use_llm: bool = False) -> dict[str, Signal]:
    """
    Fan-out per (symbol, timeframe) pair.
    
    Flow:
      1. Fetch OHLCV for all symbols × 4 timeframes
      2. Add technical indicators (EMA, RSI, MACD, BB, ATR)
      3. Classify regime (bull/bear/chop)
      4. For each (symbol, tf):
         a. Run all 6 sync agents in parallel
         b. [Optional] Run LLM agent (bounded concurrency, D1/W1 only)
      5. Aggregate signals per symbol (weighted voting)
      6. Return dict[symbol → aggregated Signal]
    """
    repo = OHLCVRepository()
    classifier = RegimeClassifier()
    raw_signals: list[Signal] = []
    
    symbols = await get_symbols()  # Dynamic or static
    for symbol in symbols:
        for tf in TIMEFRAMES:  # 1h, 4h, 1d, 1w
            df = await repo.get(symbol, tf, refresh=False)
            if df is None or len(df) < 30:
                continue  # Skip coins with <30 bars
            
            df = add_indicators(df)  # EMA, RSI, MACD, BB, ATR
            regime = classifier.classify(df)
            ctx = AgentContext(symbol, tf, df, regime)
            
            # Run 6 sync agents
            for agent in [
                TrendFollowerAgent(),
                MeanReversionAgent(),
                BreakoutAgent(),
                MomentumAgent(),
                VolatilityAgent(),
                RegimeOverlayAgent(),
            ]:
                sig = agent.analyze(ctx)
                if sig:
                    raw_signals.append(sig)
            
            # [Optional] Run LLM (bounded concurrency)
            if use_llm and tf in (Timeframe.D1, Timeframe.W1):
                sig = await LLM_AGENT.analyze_async(ctx)
                if sig:
                    raw_signals.append(sig)
    
    # Aggregate across agents/timeframes per symbol
    aggregator = SignalAggregator()
    return aggregator.aggregate(raw_signals)
```

### The 7 Agents

| Agent | Input | Logic | Confidence Range | Key Feature |
|---|---|---|---|---|
| **TrendFollower** | EMA20, EMA50, EMA200 | Cross + trend alignment | 0.4–0.7 | Crossover + distance filter |
| **MeanReversion** | RSI, Bollinger Bands | Oversold/overbought + mean revert | 0.3–0.6 | RSI > 70 SELL, < 30 BUY |
| **Breakout** | High/Low + ATR | Recent breakout + momentum | 0.5–0.8 | 20-bar high/low with ATR threshold |
| **Momentum** | ROC, MACD | Rate of change + histogram | 0.4–0.7 | MACD histogram cross + ROC |
| **Volatility** | ATR, regime | Volatility expansion signal | 0.3–0.6 | ATR spike detection + regime filter |
| **RegimeOverlay** | Regime + EMA slope | Adaptive bias per market regime | 0.2–0.8 | Boost/suppress based on bull/bear |
| **LLMReasoner** | Web context + chart snapshot | Natural language reasoning | 0.1–0.9 | Multi-provider (DeepSeek, OpenAI, Groq, etc.) |

### Agent Base Class
```python
@dataclass(frozen=True)
class AgentContext:
    symbol: str
    timeframe: Timeframe
    df: pd.DataFrame  # OHLCV with [open, high, low, close, volume]
                      # + [ema_20, ema_50, ema_200, rsi_14, macd, bb_*]
    regime: Regime    # "bull" | "bear" | "chop"

class Agent(ABC):
    name: str = "agent"
    
    @abstractmethod
    def analyze(self, ctx: AgentContext) -> Signal | None:
        """Return Signal or None if no edge detected."""
        ...
```

### Example: Trend Follower Agent
```python
class TrendFollowerAgent(Agent):
    name = "trend_follower"
    
    def analyze(self, ctx: AgentContext) -> Signal | None:
        df = ctx.df.dropna()
        if len(df) < 2:
            return None
        
        last, prev = df.iloc[-1], df.iloc[-2]
        
        # Crossover detection
        crossed_up = (prev["ema_20"] <= prev["ema_50"] and 
                      last["ema_20"] > last["ema_50"])
        crossed_dn = (prev["ema_20"] >= prev["ema_50"] and 
                      last["ema_20"] < last["ema_50"])
        
        # Trend confirmation filters
        bullish = last["ema_20"] > last["ema_50"] and last["close"] > last["ema_200"]
        bearish = last["ema_20"] < last["ema_50"] and last["close"] < last["ema_200"]
        
        if crossed_up and bullish:
            action, conf = SignalAction.BUY, 0.7
        elif crossed_dn and bearish:
            action, conf = SignalAction.SELL, 0.7
        elif bullish:
            action, conf = SignalAction.BUY, 0.4
        elif bearish:
            action, conf = SignalAction.SELL, 0.4
        else:
            return None
        
        return Signal(
            agent="trend_follower",
            symbol=ctx.symbol,
            timeframe=ctx.timeframe,
            action=action,
            confidence=conf,
            rationale=f"ema20={last['ema_20']:.2f} ema50={last['ema_50']:.2f}",
        )
```

---

## 5. Signal Aggregation (`app/signals/types.py`)

### Weighted Voting Architecture

```python
class SignalAggregator:
    def aggregate(self, signals: Iterable[Signal]) -> dict[str, Signal]:
        """
        Fuse multi-agent, multi-timeframe signals per symbol.
        
        Weighting axis 1: Timeframe
          1h=1.0, 4h=1.5, 1d=2.5, 1w=4.0  (higher TF = more weight)
        
        Weighting axis 2: Agent
          From settings.agent_weight_*  (default all 1.0)
          Examples:
            - trend_follower:   1.0 (default)
            - mean_reversion:   1.0
            - llm_reasoner:     0.8 (slightly discounted, non-deterministic)
        
        Weighting axis 3: Adaptive win-rate
          [Optional] scale by agent's rolling win-rate
          If adaptive_agent_weights=true, agent_weight *= (0.5 + win_rate)
          Range: [0.5, 1.5]
        
        Formula:
          weighted_confidence = timeframe_weight × agent_weight × 
                               adaptive_multiplier × signal.confidence
        """
        win_rates = _load_win_rates()
        
        # Accumulate votes per (symbol, action)
        scores: dict[str, dict[SignalAction, float]] = defaultdict(
            lambda: {SignalAction.BUY: 0.0, SignalAction.SELL: 0.0}
        )
        
        for sig in signals:
            tf_w = _TF_WEIGHT[sig.timeframe]
            agent_w = _agent_weight(sig.agent)
            adapt = _adaptive_multiplier(win_rates, sig.agent)
            w = tf_w * agent_w * adapt * sig.confidence
            scores[sig.symbol][sig.action] += w
        
        # Winner-take-all: action with highest score wins
        result: dict[str, Signal] = {}
        for symbol, votes in scores.items():
            action = max(votes, key=votes.__getitem__)
            total = sum(votes.values())
            confidence = min(votes[action] / total, 1.0)
            result[symbol] = Signal(
                agent="aggregator",
                symbol=symbol,
                timeframe=Timeframe.D1,
                action=action,
                confidence=confidence,
                contributing_agents=tuple(sorted(contribs[symbol][action])),
            )
        
        return result
```

---

## 6. Data Flow: OHLCV, Indicators, Regime

### Data Acquisition (`app/data/ohlcv.py`)

```python
class OHLCVRepository:
    """Fetch → cache to pickle."""
    
    async def get(
        self,
        symbol: str,
        timeframe: Timeframe,
        limit: int = 500,
        refresh: bool = True,
    ) -> pd.DataFrame:
        """
        1. On refresh=True: fetch fresh 500 candles from Binance.US
        2. Cache to {data_cache_dir}/{symbol}_{timeframe}.pkl
        3. Return pandas DataFrame indexed by close_time (UTC)
        
        Columns: open, high, low, close, volume, quote_volume, trades, open_time
        """
        path = self._path(symbol, timeframe)
        if not refresh and path.exists():
            return pd.read_pickle(path)
        
        df = await self._client.klines(symbol, timeframe, limit=limit)
        df.to_pickle(path)
        return df
```

### Indicator Pipeline (`app/ta/indicators.py`)

```python
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Inputs:  OHLCV dataframe with [open, high, low, close, volume]
    Outputs: Same DF + indicator columns
    
    Indicators added (via pandas-ta or ta library):
      - EMA (20, 50, 200)         → Trend structure
      - RSI (14)                  → Momentum / overbought-oversold
      - MACD                      → Trend + momentum
      - Bollinger Bands (20)      → Mean reversion zones
      - ATR (14)                  → Volatility + position sizing
    
    Fallback: pandas-ta (preferred) → ta library if pandas-ta fails
    
    Returns: df with columns [*original, ema_20, ema_50, ema_200, rsi_14, 
                              macd, macd_signal, macd_hist, bb_lower, bb_mid, 
                              bb_upper, atr_14]
    """
    try:
        out["ema_20"] = pta.ema(close, length=20)
        out["rsi_14"] = pta.rsi(close, length=14)
        # ... etc
    except:
        out["ema_20"] = EMAIndicator(close=close, window=20).ema_indicator()
        # ... etc
    
    return out
```

### Regime Classification (`app/regime/classifier.py`)

```python
class RegimeClassifier:
    """Heuristic (currently) regime classification."""
    
    def classify(self, df: pd.DataFrame) -> Regime:
        """
        Inputs: DataFrame with indicators (ema_20, ema_50, atr_14, close)
        
        Logic (transparent rule-based):
          1. atr_pct = atr_14 / close
          2. If atr_pct < 1.5% → choppy/noisy market → Regime.CHOP
          3. slope = ema_20 - ema_50
          4. If |slope| ≈ 0 → Regime.CHOP
          5. If slope > 0 → Regime.BULL
          6. If slope < 0 → Regime.BEAR
        
        Returns: "bull" | "bear" | "chop"
        
        ¡ Hooks in place to swap in sklearn model once labeled data exists ¡
        """
        last = df.dropna().iloc[-1]
        atr_pct = last["atr_14"] / last["close"]
        
        if atr_pct < 0.015:
            return Regime.CHOP
        
        slope = last["ema_20"] - last["ema_50"]
        if np.isclose(slope, 0):
            return Regime.CHOP
        
        return Regime.BULL if slope > 0 else Regime.BEAR
```

### Data Flow Diagram

```
┌─────────────────────────────────────────────────────────┐
│ SCHEDULER TICK                                          │
│ @ minute 2,17,32,47                                     │
└──────────────────┬──────────────────────────────────────┘
                   │
        ┌──────────▼────────────┐
        │ refresh_market_data() │
        │ (every 15 min)        │
        └──────────┬────────────┘
                   │
        ┌──────────▼──────────────────────────────────┐
        │ FOR each symbol in SYMBOLS (25 coins)      │
        │   FOR each tf in TIMEFRAMES (1h/4h/1d/1w)  │
        │     await repo.get(symbol, tf, refresh=T)  │
        └──────────┬──────────────────────────────────┘
                   │
    ┌──────────────▼──────────────────┐
    │ await client.klines()            │
    │ Fetch 500 candles from Binance   │
    │ $ HTTP GET /api/v3/klines        │
    └──────────┬───────────────────────┘
               │
    ┌──────────▼──────────────────┐
    │ df.to_pickle()              │
    │ Cache to data/cache/        │
    │ {SYMBOL}_{TF}.pkl           │
    └──────────┬──────────────────┘
               │
    ┌──────────▼──────────────────────────────────────┐
    │ autopilot.tick()                                 │
    │ (called 4 times/hour)                            │
    └──────────┬──────────────────────────────────────┘
               │
    ┌──────────▼────────────────────────────────────────┐
    │ run_all_agents()                                  │
    │ FOR each (symbol, tf):                            │
    │   df = repo.get(symbol, tf, refresh=False)        │
    │   add_indicators(df)  ← calc EMA, RSI, etc        │
    │   regime = classifier.classify(df)                │
    │   FOR each agent:                                 │
    │     sig = agent.analyze(ctx)                      │
    └──────────┬────────────────────────────────────────┘
               │
    ┌──────────▼──────────────────────────┐
    │ SignalAggregator.aggregate()         │
    │ Weighted voting by tf, agent, win-rate
    │ → dict[symbol → aggregated Signal]   │
    └──────────┬──────────────────────────┘
               │
    ┌──────────▼────────────────────────────┐
    │ autopilot._execute(signals)           │
    │ Apply all decision gates (§4.1)       │
    │ Place BUY/SELL orders                 │
    │ Record to positions + orders tables   │
    └──────────────────────────────────────┘
```

---

## 7. Exchange Integration (`app/exchange/*`)

### Architecture: Single Point of Truth

All Binance.US interaction flows through `app/exchange/client.py`. This isolation enables:
- Paper mode (mock via SQLite)
- Dry-run mode (log, no-op order)
- Live mode (real orders)

```python
class BinanceUSClient:
    """Thin async wrapper around binance-connector."""
    
    def __init__(self, settings: Optional[Settings] = None):
        self._settings = settings or get_settings()
        self._spot = Spot(
            api_key=self._settings.binance_api_key.get_secret_value() or None,
            api_secret=self._settings.binance_api_secret.get_secret_value() or None,
            base_url="https://api.binance.us",
        )
    
    # ── Market Data ──
    async def klines(self, symbol: str, timeframe: Timeframe, 
                     limit: int = 500) -> pd.DataFrame:
        """Fetch OHLCV candles (public endpoint)."""
        raw = await asyncio.to_thread(
            self._spot.klines, symbol, timeframe.value, limit=limit
        )
        return self._parse_klines(raw)
    
    async def ticker_price(self, symbol: str) -> Decimal:
        """Current market price."""
        data = await asyncio.to_thread(self._spot.ticker_price, symbol)
        return Decimal(str(data["price"]))
    
    async def order_book(self, symbol: str, limit: int = 10) -> dict:
        """L2 order book snapshot (public)."""
        return await asyncio.to_thread(self._spot.depth, symbol, limit=limit)
    
    async def account(self) -> dict:
        """Portfolio balances (authed endpoint)."""
        return await asyncio.to_thread(self._spot.account)
    
    # ── Order Placement ──
    async def place_order(
        self,
        symbol: str,
        side: OrderSide,      # BUY | SELL
        type: OrderType,      # MARKET | LIMIT
        quantity: Decimal,
        price: Optional[Decimal] = None,
        client_order_id: Optional[str] = None,
    ) -> Order:
        """
        Place an order. Honors Settings.dry_run/paper_trading.
        
        Dry-run: log + return DRY_RUN status
        Paper: simulate against live ticker (SQLite paper_balances)
        Live: submit to Binance.US
        """
        coid = client_order_id or _new_client_order_id()
        
        if self._settings.dry_run or self._settings.paper_trading:
            log.warning("[DRY-RUN] %s %s qty=%s", symbol, side.value, quantity)
            return Order(..., status=OrderStatus.DRY_RUN)
        
        # Live order submission
        params = {
            "symbol": symbol,
            "side": side.value,
            "type": type.value,
            "quantity": str(quantity),
            "newClientOrderId": coid,
        }
        raw = await asyncio.to_thread(self._spot.new_order, **params)
        return Order(..., status=OrderStatus(raw["status"]), raw=raw)
    
    async def liquidate_all(self) -> list[Order]:
        """Market-sell every non-USDT balance into USDT."""
        account = await self.account()
        results: list[Order] = []
        for bal in account["balances"]:
            if bal["asset"] == "USDT" or Decimal(bal["free"]) <= 0:
                continue
            symbol = f"{bal['asset']}USDT"
            await self.place_order(
                symbol=symbol,
                side=OrderSide.SELL,
                type=OrderType.MARKET,
                quantity=Decimal(bal["free"]),
            )
        return results
```

### Exchange Filters (`app/exchange/filters.py`)

```python
class SymbolFilters:
    """Load Binance.US exchangeInfo once; use to validate orders."""
    
    async def load(self) -> None:
        """Fetch exchangeInfo, extract LOT_SIZE + MIN_NOTIONAL per symbol."""
        data = await asyncio.to_thread(Spot(...).exchange_info)
        for sym in data["symbols"]:
            entry = {"status": sym["status"]}
            for f in sym["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    entry["step_size"] = Decimal(f["stepSize"])
                elif f["filterType"] in ("MIN_NOTIONAL", "NOTIONAL"):
                    entry["min_notional"] = Decimal(f.get("minNotional", "0"))
            self._info[sym["symbol"]] = entry
    
    def round_qty(self, symbol: str, qty: Decimal) -> Decimal:
        """Round qty down to LOT_SIZE step."""
        info = self._info.get(symbol) or {}
        step = info.get("step_size")
        if step and step > 0:
            qty = (qty / step).quantize(Decimal("1"), ROUND_DOWN) * step
        return qty
    
    def meets_min(self, symbol: str, qty: Decimal, price: Decimal) -> bool:
        """Check qty ≥ MIN_QTY and notional ≥ MIN_NOTIONAL."""
        info = self._info.get(symbol) or {}
        min_qty = info.get("min_qty")
        min_notional = info.get("min_notional")
        
        if min_qty and qty < min_qty:
            return False
        if min_notional and (qty * price) < min_notional:
            return False
        return True

filters = SymbolFilters()  # Singleton, loaded once at startup
```

### Paper Exchange (`app/trading/paper.py`)

```python
class PaperExchange:
    """Mock exchange that simulates fills against live ticker."""
    
    async def place_order(
        self,
        *,
        symbol: str,
        side: OrderSide,
        quantity: Decimal,
        agents: Optional[list[str]] = None,
        client_order_id: Optional[str] = None,
    ) -> Order:
        """
        Simulate a market order using current live ticker price.
        Debit/credit paper balances in SQLite.
        """
        price = await self._live.ticker_price(symbol)
        notional = quantity * price
        fee = notional * Decimal(str(get_settings().binance_taker_fee))
        base = symbol.removesuffix("USDT")
        
        if side is OrderSide.BUY:
            usdt_needed = notional + fee
            usdt_have = Decimal(str(storage.paper_balance_get("USDT")))
            if usdt_have < usdt_needed:
                raise RuntimeError("insufficient paper USDT")
            
            storage.paper_balance_add("USDT", -usdt_needed)
            storage.paper_balance_add(base, quantity)
            storage.open_position(
                symbol=symbol, mode="paper", qty=quantity,
                entry_price=price, agents=agents or [],
            )
        else:  # SELL
            qty = Decimal(storage.paper_balance_debit(base, quantity))
            proceeds = qty * price - (qty * price * fee_rate)
            storage.paper_balance_add("USDT", proceeds)
            storage.close_position(symbol=symbol, exit_price=price)
        
        storage.record_order(
            mode="paper", symbol=symbol, side=side.value,
            qty=quantity, price=price, fee=fee, agents=agents or [],
        )
        return Order(..., status=OrderStatus.FILLED, avg_fill_price=price)

paper_exchange = PaperExchange()
```

---

## 8. Risk Management (`app/trading/risk.py`)

### Five Hard Exit Rules (Evaluated BEFORE Agents)

```python
def evaluate_exits(
    *,
    positions: list[dict],
    prices: dict[str, Decimal],
    now: Optional[datetime] = None,
) -> list[ExitDecision]:
    """
    Inspect every open position. Return those hitting ANY hard rule.
    These are FORCED exits — no agent signal needed.
    """
    s = get_settings()
    exits: list[ExitDecision] = []
    
    for pos in positions:
        symbol = pos["symbol"]
        price = prices.get(symbol)
        entry = Decimal(str(pos["entry_price"]))
        qty = Decimal(str(pos["qty"]))
        change = (price - entry) / entry  # % return
        
        # ── Rule 1: Hard Stop Loss ──
        if change <= Decimal(str(-s.stop_loss_pct)):  # default -5%
            exits.append(ExitDecision(symbol, qty, "stop_loss"))
            continue
        
        # ── Rule 2: Take Profit ──
        if change >= Decimal(str(s.take_profit_pct)):  # default +15%
            exits.append(ExitDecision(symbol, qty, "take_profit"))
            continue
        
        # ── Rule 3: Trailing Stop ──
        # Only armed after position gains 50% of take-profit target
        hwm = update_hwm(symbol, price)
        half_tp = Decimal(str(s.take_profit_pct)) / Decimal("2")
        if hwm > entry * (Decimal("1") + half_tp):
            trail_floor = hwm * (Decimal("1") - Decimal(str(s.trailing_stop_pct)))
            if price <= trail_floor:
                exits.append(ExitDecision(symbol, qty, "trailing_stop"))
                continue
        
        # ── Rule 4: Max Hold Time ──
        entry_ts = datetime.fromisoformat(pos["entry_ts"])
        if now - entry_ts > timedelta(hours=s.max_hold_hours):  # default 168h = 7d
            exits.append(ExitDecision(symbol, qty, "max_hold"))
```

### Drawdown Circuit Breaker

```python
def is_circuit_breaker_tripped(
    *,
    starting_balance: Optional[Decimal],
    current_balance: Decimal,
) -> tuple[bool, float]:
    """
    Rule 5: Drawdown breaker → halt new BUYs when DD < threshold.
    
    Used to prevent average-down in a losing streak.
    Does NOT force exits, only blocks NEW entries.
    """
    if not starting_balance or starting_balance <= 0:
        return False, 0.0
    
    dd = (current_balance - starting_balance) / starting_balance
    threshold = Decimal(str(get_settings().drawdown_circuit_breaker_pct))
    return (dd <= -threshold), float(dd)
    # e.g. if starting=$10k, current=$9.7k → DD=-3% → tripped if threshold=3%
```

### Position Sizing

```python
def volatility_scaled_pct(
    base_pct: float,
    atr_pct: Optional[float],
    *,
    target_atr_pct: float = 0.020,  # 2% daily move = avg volatility
    floor: float = 0.5,
    ceiling: float = 1.5,
) -> float:
    """
    Scale position size inversely with volatility.
    Quiet coins (1% ATR) → bigger size, noisy coins (3% ATR) → smaller size.
    
    multiplier = clamp(target_atr_pct / atr_pct, floor, ceiling)
    eff_pct = base_pct * multiplier
    """
    if not atr_pct or atr_pct <= 0:
        return base_pct
    raw = target_atr_pct / atr_pct
    mult = max(floor, min(ceiling, raw))
    return base_pct * mult
```

### Position Cap Enforcer

```python
def can_open_new_position(
    *,
    open_positions: int,
    long_exposure_pct: float,
) -> tuple[bool, str]:
    """
    Two limits:
      1. Max concurrent open positions (e.g., 5)
      2. Max non-USDT exposure (e.g., 70% of equity)
    """
    s = get_settings()
    if open_positions >= s.max_open_positions:
        return False, f"max_open_positions={s.max_open_positions} reached"
    if long_exposure_pct >= s.max_long_exposure_pct:
        return False, f"exposure {long_exposure_pct:.0%} >= {s.max_long_exposure_pct:.0%}"
    return True, ""
```

---

## 9. ML-Driven Quality Gate (`app/regime/trainer.py` + `online.py`)

### Learning Cycle Orchestration

```python
async def run_learning_cycle() -> dict:
    """
    Called every 6 hours by scheduler.
    
    1. Label matured signal events (older than horizon_minutes)
    2. Retrain signal_quality_v1 model if enough new labels exist
    3. Persist to model artifact store
    """
    s = get_settings()
    if not s.ml_learning_enabled:
        return {"status": "disabled"}
    
    # ── Stage 1: Label Matured Events ──
    horizon = timedelta(minutes=s.ml_signal_horizon_minutes)
    cutoff = (datetime.now(timezone.utc) - horizon).isoformat()
    pending = storage.pending_signal_events(older_than_iso=cutoff, limit=1000)
    
    for ev in pending:
        px_now = await get_current_price(ev["symbol"])
        ret = (px_now - ev["entry_price"]) / ev["entry_price"]
        win = ret > 0.001  # 0.1% profit = win (dead zone)
        storage.resolve_signal_event(
            event_id=ev["id"],
            outcome_return_pct=ret,
            outcome_win=win,
        )
    
    # ── Stage 2: Retrain If Enough Data ──
    total_resolved = storage.count_resolved_signal_events()
    since_last_train = total_resolved - storage.kv_get(_LAST_TRAINED_RESOLVED_KEY, 0)
    
    if since_last_train >= s.ml_min_new_labels:
        result = train_signal_quality_model()
        storage.kv_set(_LAST_TRAINED_RESOLVED_KEY, total_resolved)
        return result
    
    return {"status": "labeled_only", "new_labels": since_last_train}
```

### Signal Quality Model Training

```python
def train_signal_quality_model() -> dict:
    """
    Train a lightweight logistic regression classifier:
    
    INPUT FEATURES (7D):
      1. Signal confidence (0–1)
      2. ATR % (volatility)
      3. RSI (momentum)
      4. EMA gap % (trend strength)
      5. Agent count (signal sources)
      6. Timeframe weight (1h=1, 4h=1.5, 1d=2.5, 1w=4)
      7. Action (BUY=1, SELL=0)
    
    LABEL: Binary win/loss from resolve_signal_event()
    
    OUTPUT: sklearn Pipeline saved as model artifact "signal_quality_v1"
    """
    s = get_settings()
    rows = storage.training_signal_rows(limit=100_000)
    
    if len(rows) < s.ml_min_training_samples:
        return {"status": "insufficient_data"}
    
    x, y = _rows_to_xy(rows)
    x_train, x_test, y_train, y_test = train_test_split(
        x, y, test_size=0.2, random_state=42, stratify=y
    )
    
    model = Pipeline([
        ("scaler", StandardScaler()),
        ("clf", LogisticRegression(max_iter=1000, class_weight="balanced")),
    ])
    model.fit(x_train, y_train)
    
    metrics = {
        "accuracy": accuracy_score(y_test, model.predict(x_test)),
        "roc_auc": roc_auc_score(y_test, model.predict_proba(x_test)[:, 1]),
        "positive_rate": float(y.mean()),
    }
    version = storage.save_model_artifact(
        name="signal_quality_v1",
        algorithm="logistic_regression",
        metrics=metrics,
        model=model,
    )
    return {"status": "ok", "version": version, **metrics}
```

### Dynamic Confidence Threshold (`app/regime/online.py`)

```python
class OnlineRegime:
    """
    Light online model that adjusts entry bar per market regime.
    Nudges min_signal_confidence up/down based on recent trade outcomes.
    """
    
    def threshold_delta(self) -> tuple[float, str]:
        """
        Return (delta, info) where delta ∈ [-max, +max].
        
        Applied as: effective_min_conf = base_min_conf + delta
        """
        rows = storage.training_signal_rows(limit=100)  # last 100 trades
        if len(rows) < 20:
            return 0.0, "insufficient_data"
        
        # Features: [atr_pct, ema_gap_pct, rsi_14, confidence]
        x = np.asarray([...], dtype=float)
        y = np.asarray([int(r["outcome_win"]) for r in rows])
        
        # Fit small logistic model
        model = make_pipeline(StandardScaler(), LogisticRegression(...))
        model.fit(x, y)
        p_hat = model.predict_proba(x)[:, 1].mean()
        
        # Regime favorability = blend predicted win prob + realized win rate
        win_rate = y.mean()
        favorability = 0.5 * p_hat + 0.5 * win_rate
        
        # Penalize high volatility (risk-off)
        vol = np.median(x[:, 0])
        vol_penalty = min(0.10, max(0.0, (vol - 0.03) * 1.0))
        favorability = max(0.0, min(1.0, favorability - vol_penalty))
        
        # Map favorability [0, 1] → delta [-max, +max]
        # favorability=0.5 → delta=0 (neutral)
        max_delta = get_settings().dynamic_threshold_max_delta
        delta = max_delta * (1.0 - 2.0 * favorability)
        delta = max(-max_delta, min(max_delta, delta))
        
        return delta, f"favorability={favorability:.2f} vol={vol:.3f}"
```

---

## 10. Persistence (`app/storage/db.py`)

### SQLite Schema Overview

```python
_SCHEMA = """
-- ── KV Store (autopilot state) ──
CREATE TABLE kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ── Trading Records ──
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,            -- "paper" | "live"
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,            -- "BUY" | "SELL"
    qty REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL DEFAULT 0,
    client_order_id TEXT,
    agents TEXT                    -- JSON list of agent names
);

CREATE TABLE positions (
    symbol TEXT PRIMARY KEY,
    mode TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    entry_ts TEXT NOT NULL,
    agents TEXT NOT NULL          -- JSON list
);

CREATE TABLE closed_trades (
    id INTEGER PRIMARY KEY,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    qty REAL NOT NULL,
    entry_price REAL NOT NULL,
    exit_price REAL NOT NULL,
    pnl REAL NOT NULL,
    pnl_pct REAL NOT NULL,
    entry_ts TEXT NOT NULL,
    exit_ts TEXT NOT NULL,
    agents TEXT NOT NULL
);

-- ── Agent Statistics (for adaptive weighting) ──
CREATE TABLE agent_stats (
    agent TEXT PRIMARY KEY,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    total_pnl REAL NOT NULL DEFAULT 0,
    last_updated TEXT
);

-- ── ML Signal Events (for training) ──
CREATE TABLE ml_signal_events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    action TEXT NOT NULL,            -- "BUY" | "SELL"
    confidence REAL NOT NULL,
    entry_price REAL NOT NULL,
    atr_pct REAL,
    rsi_14 REAL,
    ema_gap_pct REAL,
    agent_count INTEGER NOT NULL DEFAULT 0,
    resolved INTEGER NOT NULL DEFAULT 0,  -- boolean: labeled?
    resolved_ts TEXT,
    horizon_minutes INTEGER,
    outcome_return_pct REAL,
    outcome_win INTEGER              -- boolean: win or loss?
);

-- ── Model Artifacts ──
CREATE TABLE ml_models (
    name TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    trained_at TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    metrics TEXT NOT NULL,           -- JSON
    payload BLOB NOT NULL            -- pickle
);

-- ── Paper Account ──
CREATE TABLE paper_balances (
    asset TEXT PRIMARY KEY,
    qty REAL NOT NULL
);

-- ── Portfolio Tracking ──
CREATE TABLE equity_snapshots (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,
    total_usdt REAL NOT NULL,
    cash_usdt REAL NOT NULL,
    invested_usdt REAL NOT NULL
);

-- ── Authentication ──
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    email_verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_login_at TEXT,
    failed_attempts INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT
);

CREATE TABLE auth_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    purpose TEXT NOT NULL,    -- "verify" | "reset"
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    ip TEXT,
    user_agent TEXT
);

CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    user_id INTEGER,
    ip TEXT,
    action TEXT NOT NULL,
    detail TEXT
);
"""
```

### Key Storage Functions

```python
class Storage:
    def kv_get(self, key: str) -> Any:
        """Retrieve JSON value from KV store (autopilot state, etc)."""
    
    def kv_set(self, key: str, value: Any) -> None:
        """Persist JSON value to KV store."""
    
    def open_position(self, symbol: str, mode: str, qty: Decimal, 
                     entry_price: Decimal, agents: list[str]) -> None:
        """Record new open position."""
    
    def close_position(self, symbol: str, exit_price: Decimal) -> None:
        """Move position to closed_trades, update agent stats."""
    
    def record_order(self, mode: str, symbol: str, side: str, qty: Decimal,
                    price: Decimal, fee: Decimal, agents: list[str]) -> None:
        """Log executed order."""
    
    def record_signal_event(self, symbol: str, action: str, confidence: float,
                           entry_price: Decimal, tf: str, 
                           atr_pct: float, rsi_14: float, ema_gap: float) -> int:
        """Save signal for later labeling."""
    
    def resolve_signal_event(self, event_id: int, outcome_return_pct: float,
                            outcome_win: bool) -> None:
        """Label a signal event with outcome."""
    
    def pending_signal_events(self, older_than_iso: str, 
                             limit: int = 500) -> list[dict]:
        """Fetch unresolved signal events past horizon."""
    
    def training_signal_rows(self, limit: int) -> list[dict]:
        """Fetch resolved signal events (features + labels for ML)."""
    
    def save_model_artifact(self, name: str, algorithm: str, 
                           metrics: dict, model: Any) -> int:
        """Persist trained model (pickle + metadata)."""
    
    def load_model_artifact(self, name: str) -> Optional[dict]:
        """Load trained model by name."""
    
    def try_acquire_lock(self, key: str, ttl_seconds: float, 
                        owner: str) -> bool:
        """Cross-process mutex (via KV store with TTL)."""
    
    def release_lock(self, key: str, owner: str) -> None:
        """Release lock when done."""
    
    def paper_balances(self) -> dict[str, float]:
        """Get current paper account balances."""
    
    def paper_balance_get(self, asset: str) -> float:
        """Get qty of one asset in paper account."""
    
    def paper_balance_add(self, asset: str, qty: Decimal) -> None:
        """Debit/credit one asset in paper account."""
    
    def all_positions(self) -> list[dict]:
        """All open positions (paper + live, combined)."""
```

---

## 11. Web API (`app/api/routes.py`)

### Dashboard Endpoints

| Endpoint | Method | Purpose | Returns |
|---|---|---|---|
| `/` | GET | Main dashboard | HTML (portfolio, P&L, agent stats) |
| `/trades` | GET | Trade history | HTML table (closed trades, agent attribution) |
| `/settings` | GET | Settings form | HTML form |
| `/settings` | POST | Save settings | Redirect to `/` |
| `/start` | POST | Start autopilot | Redirect to `/` |
| `/stop` | POST | Stop + liquidate | Redirect to `/` |
| `/healthz` | GET | Health check | `{"status": "ok"}` |

### Authentication Routes (`app/auth/routes.py`)

| Endpoint | Method | Purpose |
|---|---|---|
| `/auth/login` | GET | Login form |
| `/auth/login` | POST | Submit credentials |
| `/auth/verify/{token}` | GET | Verify email link |
| `/auth/password` | GET | Change password form |
| `/auth/password` | POST | Change password |
| `/auth/logout` | GET | Sign out |
| `/auth/audit` | GET | Audit log (IP, action, timestamp) |

### Dashboard State Display

```html
<!-- Portfolio Card -->
<div class="card">
  <h2>Portfolio {PAPER|LIVE}</h2>
  Total Balance: ${total_usdt}
  USDT Cash: ${usdt_cash}
  Invested: ${invested_usdt}
  [P&L since start: ${pnl} ({pnl_pct}%)]
  [Top 5 holdings]
</div>

<!-- Agent Stats -->
<table>
  <tr><th>Agent</th><th>Total Trades</th><th>Win Rate</th><th>Total P&L</th></tr>
  <tr><td>trend_follower</td><td>42</td><td>62.3%</td><td>+$1,242.50</td></tr>
  ...
</table>

<!-- Controls -->
[START AUTOPILOT] [STOP & LIQUIDATE]
[Mode: {PAPER|LIVE}] [Switch Mode]
```

---

## 12. Configuration (`app/config.py`)

### Settings (pydantic v2)

```python
class Settings(BaseSettings):
    # ── Universe ──
    use_dynamic_symbols: bool = True
    static_symbols: tuple[str, ...] = STATIC_SYMBOLS  # 25 coins fallback
    max_symbols: int = 100
    
    # ── Risk Caps ──
    max_open_positions: int = 5
    max_long_exposure_pct: float = 0.70      # 70% of equity
    max_position_pct: float = 0.15           # 15% per trade (pre-vol-scaling)
    stop_loss_pct: float = 0.05              # -5%
    take_profit_pct: float = 0.15            # +15%
    trailing_stop_pct: float = 0.02          # 2% from HWM
    max_hold_hours: int = 168                # 7 days
    drawdown_circuit_breaker_pct: float = 0.03  # -3%
    
    # ── Agent Weights ──
    agent_weight_trend_follower: float = 1.0
    agent_weight_mean_reversion: float = 1.0
    agent_weight_breakout: float = 1.0
    agent_weight_momentum: float = 1.0
    agent_weight_volatility: float = 1.0
    agent_weight_regime_overlay: float = 1.0
    agent_weight_llm_reasoner: float = 0.8   # Discounted (non-deterministic)
    
    # ── Dynamic Threshold ──
    dynamic_threshold_enabled: bool = True
    dynamic_threshold_max_delta: float = 0.10  # ±10% swing on min_conf
    
    # ── ML Gate ──
    ml_gate_enabled: bool = True
    ml_gate_threshold: float = 0.55           # P(win) ≥ 55% to execute
    ml_gate_max_model_age_hours: int = 24     # Fail-open if model stale
    ml_learning_enabled: bool = True
    ml_signal_horizon_minutes: int = 120      # 2h window to label signals
    ml_min_training_samples: int = 100
    ml_min_new_labels: int = 20
    
    # ── LLM ──
    llm_provider: str = "deepseek"  # or openai, groq, gemini, github
    llm_in_trading_loop: bool = True           # Include LLM in autopilot votes
    llm_web_enabled: bool = True
    
    # ── Trading ──
    paper_trading: bool = True                # Start in paper mode
    dry_run: bool = False                      # Log orders, don't submit
    buy_cooldown_minutes: int = 60             # Min gap between BUY signals
    min_signal_confidence: float = 0.65        # Base entry threshold
    
    # ── Binance.US ──
    binance_api_key: SecretStr = SecretStr("")
    binance_api_secret: SecretStr = SecretStr("")
    binance_base_url: str = "https://api.binance.us"
    binance_taker_fee: float = 0.001           # 0.1%

@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=_ENV_PATH)

# Constants
TIMEFRAMES = (Timeframe.H1, Timeframe.H4, Timeframe.D1, Timeframe.W1)
SYMBOLS = STATIC_SYMBOLS  # 25 coins
```

---

## 13. Complete Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         COMPLETE ARCHITECTURE                               │
└─────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────┐
│ USER / OPERATOR                     │
│ (Dashboard HTML)                    │
└──────────────┬──────────────────────┘
               │
      [Click START/STOP/SETTINGS]
               │
     ┌─────────▼──────────┐
     │ FastAPI + Sessions │
     │ (app/main.py)      │
     └─────────┬──────────┘
               │
    ┌──────────▼──────────────┐
    │ APScheduler (5 jobs)    │
    │ (:02,:17,:32,:47 ticks) │
    │ (:07 LLM, :12 ML, :55 EQ)
    └──────────┬──────────────┘
               │
    ┌──────────▼────────────────────────────────────┐
    │ Scheduler @ minute 2, 17, 32, 47              │
    │ (15 min before data, 4× per hour trading)     │
    └──────────┬────────────────────────────────────┘
               │
    ┌──────────▼───────────────────────────────────────┐
    │ refresh_market_data()                             │
    │ FOR each symbol (25) × timeframe (4):            │
    │   await repo.get(symbol, tf, refresh=True)       │
    │   └─ Fetch 500 candles from Binance.US REST API  │
    │   └─ Cache to pickle: data/cache/{S}_{TF}.pkl    │
    └──────────┬───────────────────────────────────────┘
               │
    ┌──────────▼───────────────────────────────────────┐
    │ autopilot.tick()                                  │
    │ (3-4 min after data refresh)                      │
    └──────────┬───────────────────────────────────────┘
               │
    ┌──────────▼──────────────────────────────────────────────┐
    │ ATOMIC TRANSACTION (cross-process lock)                │
    │                                                         │
    │ 1. Risk Gates (async)                                  │
    │    ├─ Fetch current prices                             │
    │    ├─ Check stop-loss, take-profit, trailing, max-hold │
    │    ├─ FORCE SELL any hitting threshold                 │
    │    └─ Clear high-water-marks                           │
    │                                                         │
    │ 2. Circuit Breaker Check                               │
    │    ├─ Get portfolio snapshot                           │
    │    ├─ Compute drawdown vs starting balance             │
    │    └─ If DD < -3% → allow_buys = False                │
    │                                                         │
    │ 3. Run All Agents (async)                              │
    │    ├─ Load OHLCV from pickle cache                     │
    │    ├─ Add indicators: EMA, RSI, MACD, BB, ATR          │
    │    ├─ Classify regime (bull/bear/chop)                │
    │    └─ FOR each agent:                                  │
    │        ├─ Sync agents: analyze(ctx) → Signal | None   │
    │        └─ LLM agent: async analyze + ML-gate check    │
    │                                                         │
    │ 4. Aggregate Signals (weighted voting)                 │
    │    ├─ TF weight: 1h=1.0, 4h=1.5, 1d=2.5, 1w=4.0       │
    │    ├─ Agent weight: from settings (default all 1.0)    │
    │    ├─ Adaptive multiplier: scale by agent win-rate     │
    │    └─ Per symbol: max vote action wins                 │
    │        (confidence = max_score / sum_all_scores)       │
    │                                                         │
    │ 5. Execute Signals (ranked by confidence DESC)         │
    │    FOR each symbol:                                    │
    │      IF action == HOLD → skip                          │
    │      IF confidence < min_conf → skip                   │
    │      IF ML model P(win) < threshold → skip (gate)      │
    │      [Record signal for learning]                      │
    │      IF action == BUY:                                 │
    │        ├─ Check: breaker, already_held, cooldown       │
    │        ├─ Check: max_positions, max_exposure           │
    │        ├─ Check: trend_gate, funding_gate, onchain_gate│
    │        ├─ Check: orderbook, notional, filter           │
    │        └─ Place BUY → allocate cash                    │
    │      IF action == SELL:                                │
    │        ├─ Check: balance exists, position open         │
    │        └─ Place SELL → free up cash                    │
    │                                                         │
    │ 6. Save State (finally)                                │
    │    └─ Persist autopilot.state to KV store              │
    └──────────┬──────────────────────────────────────────────┘
               │
    ┌──────────▼──────────────────────────────────┐
    │ Order Execution Path                        │
    │                                              │
    │ Paper Mode:                                  │
    │   ├─ Get live ticker price (async)          │
    │   ├─ Debit/credit paper_balances in SQLite  │
    │   ├─ Record to orders + positions tables    │
    │   └─ Return OrderStatus.FILLED              │
    │                                              │
    │ Live Mode:                                   │
    │   ├─ Validate filters (LOT_SIZE, MIN_NOTIONAL)
    │   ├─ POST /api/v3/order to Binance.US       │
    │   ├─ Record to orders + positions tables    │
    │   └─ Return OrderStatus from Binance        │
    │                                              │
    │ [Both modes write to shared DB → learning]  │
    └──────────┬──────────────────────────────────┘
               │
    ┌──────────▼────────────────────────────┐
    │ Persistence (SQLite)                  │
    │                                        │
    │ Tables:                                │
    │  • orders (timestamp, symbol, side...) │
    │  • positions (open positions + mode)   │
    │  • closed_trades (realized P&L)        │
    │  • agent_stats (wins, losses, PnL)    │
    │  • ml_signal_events (signals + labels) │
    │  • ml_models (trained artifacts)       │
    │  • kv (state, HWM, locks)             │
    │  • paper_balances (simulated account)  │
    │  • equity_snapshots (portfolio curve)  │
    └────────────────────────────────────────┘

┌─────────────────────────────────────┐
│ @ minute 7 (hourly LLM pass)         │
│                                     │
│ run_all_agents(use_llm=True)        │
│ → Stash aggregated LLM signals      │
│   in KV store (not for trading)     │
│ → Dashboard displays for review     │
└─────────────────────────────────────┘

┌─────────────────────────────────────┐
│ @ minute 12 (every 6h ML learning)  │
│                                     │
│ run_learning_cycle():               │
│  1. label_matured_signal_events()   │
│  2. train_signal_quality_model()    │
│  3. Save model artifact             │
│ → Picks up at next autopilot tick   │
└─────────────────────────────────────┘

┌─────────────────────────────────────┐
│ @ minute 55 (hourly equity snapshot)│
│                                     │
│ equity_snapshot():                  │
│  ├─ Fetch portfolio snapshot        │
    ├─ Record (total, cash, invested) │
│  └─ Store in equity_snapshots table │
│ → Dashboard plots P&L curve         │
└─────────────────────────────────────┘
```

---

## 14. Critical Decision Points Summary

### Ordering of Gates (Why Sequential Matters)

**All gates are applied in order; first failure → SKIP signal:**

1. **Action gate** (HOLD → always skip)
2. **Confidence gate** (min_signal_confidence)
3. **ML quality gate** (P(win) threshold, only if model loaded)
4. **Symbol listed gate** (exchange status)
5. **Buy-specific gates** (if action == BUY):
   - Breaker tripped
   - Already held
   - Cooldown active
   - Max positions cap
   - Max long exposure cap
   - Trend gate (close < ema_200)
   - Funding gate (perpetuals rate)
   - On-chain whale gate
   - Orderbook liquidity gate
   - Notional/LOT_SIZE filters
6. **Sell-specific gates** (if action == SELL):
   - Balance available
   - Open position exists

### Learning Pipeline Integration

```
┌───────────────────────────────────┐
│ Signal recorded BEFORE ML gate    │ ← Prevents self-starvation
│ (in _execute → _record_signal)    │
├───────────────────────────────────┤
│ ML gate applied AFTER recording   │
│ (may reject this signal)           │
├───────────────────────────────────┤
│ 2 hours later (horizon_minutes):  │
│ Label matured signal_events       │ ← Actual outcome measured
│ (win/loss determined)              │
├───────────────────────────────────┤
│ Every 6 hours: retrain model if   │
│ enough new labels (≥20)            │
└───────────────────────────────────┘
```

---

## 15. Key Metrics & Observability

### KV Store Keys (for monitoring)

| Key | Purpose | Type |
|---|---|---|
| `autopilot_state` | Autopilot running status, mode, balance | JSON |
| `llm_signals` | Latest LLM-only signal aggregation | JSON |
| `llm_meta` | LLM provider, last run, error | JSON |
| `hwm:{symbol}` | High-water mark per position | Decimal (string) |
| `ml_gate_stats` | Gate evaluations, acceptances, probabilities | JSON |
| `autopilot_skip_stats` | Per-tick skip counter (diagnostics) | JSON |
| `autopilot_last_tick_debug` | Last tick decisions per symbol | JSON |

### Database Indices

- `orders(ts)` ← Fast lookups by time
- `closed_trades(exit_ts)` ← Equity curve
- `ml_signal_events(resolved, ts)` ← Pending labeling
- `sessions(user_id)` ← Auth lookups
- `audit_log(ts)` ← Compliance

---

## 16. Failure Modes & Safeguards

| Failure | Guard | Fallback |
|---|---|---|
| **LLM provider down** | Health check at startup; fail-open in tick | Rule-based agents only |
| **Exchange filters not loaded** | `filters.load()` called in tick startup | Filter checking disabled, risky |
| **Paper account corrupted** | Atomic balance operations via SQLite | Manual reset via script |
| **Model too old** | Staleness check (`age_h > max_model_age_hours`) | Fail-open, all signals pass |
| **Insufficient liquidity** | Orderbook gate + MIN_NOTIONAL filter | Signal skipped, no order |
| **Concurrent ticks (crash)** | Cross-process lock + TTL | Lock released after 5 min |
| **Signal never resolves** | Horizon timeout + manual labeling tools | Signals in `resolved=0` stay pending |
| **Negative balance (paper)** | Atomic paper_balance_debit() clamps to free | Transaction rolls back |

---

## 17. Quick Reference: Function Entry Points

### For a Trade to Execute

```
START
  │
  ├─ storage.try_acquire_lock("autopilot_tick", ttl=300s, owner=process_id)
  │  └─ ✗ SKIP if lock held by another process (race prevention)
  │
  ├─ await self._run_risk_gates()
  │  └─ FORCE SELL if any position hits stop-loss/TP/trailing/max-hold
  │
  ├─ await self._check_circuit_breaker()
  │  └─ ✓ breaker_tripped = (portfolio_dd < -3%)
  │
  ├─ signals = await run_all_agents(use_llm=True/False)
  │  └─ dict[symbol → Signal] with aggregated confidence
  │
  ├─ FOR each symbol (ranked by confidence DESC):
  │  │
  │  ├─ IF signal.action == HOLD → continue
  │  ├─ IF signal.confidence < 0.65 → continue
  │  ├─ [Record signal for ML learning]
  │  ├─ IF ML_model P(win) < 0.55 → continue
  │  ├─ IF action == BUY and allow_buys:
  │  │  ├─ Check all BUY gates (see table above)
  │  │  └─ await self._place_buy(symbol, sig, position_size)
  │  │     ├─ paper mode: update paper_balances, record order
  │  │     └─ live mode: POST /api/v3/order, record order
  │  │
  │  └─ IF action == SELL:
  │     ├─ Check balance exists + position open
  │     └─ await self._place_sell(symbol, sig, qty_free)
  │
  └─ storage.release_lock("autopilot_tick", owner=process_id)

END
```

---

## Summary

This system is a **production-grade autonomous trading engine** with:

✅ **Atomic execution** (cross-process lock prevents duplicate orders)  
✅ **Multi-agent consensus** (7 sources, weighted voting)  
✅ **Regime-adaptive** (EMA-based classification + online model)  
✅ **ML-gated** (logistic regression quality filter, learns from outcomes)  
✅ **Risk-first** (hard stops before agent signals)  
✅ **Paper-to-live** (seamless mode switch, learning carries over)  
✅ **Transparent** (every gate logged, skip stats, audit trail)  
✅ **Resilient** (idempotent orders, TTL locks, graceful degradation)

The **critical path** for any trade is:
```
OHLCV → Indicators → Regime → Agents → Aggregation → Decision Tree → Exchange
   1h        1h        1h       1s        <1s         ~10s          50ms
```

All code is **money-handling**. Bias is **correctness** > speed. 🎯
