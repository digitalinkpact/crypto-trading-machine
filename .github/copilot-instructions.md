# Project Guidelines — AI Crypto Trading Machine

> **Status:** Greenfield. Repo currently contains only [README.md](README.md) and [requirements.txt](requirements.txt). All paths below are *target* layout — create them as you go and update this file when reality diverges.

## What this project is

AI-driven crypto trading system on **Binance.US**. Seven cooperating agents monitor **25 coins** across **4 timeframes**, fuse TA + LLM reasoning + an ML regime classifier into signals, size positions with Kelly, and (optionally) gate trades through a `vectorbt`/`jesse` backtest before sending orders.

This is **money-handling code**. Bias every decision toward correctness, idempotency, and reversibility over cleverness or speed.

## Stack (pinned in [requirements.txt](requirements.txt) — do not swap)

| Concern | Library | Notes |
|---|---|---|
| API / server | `fastapi` 0.111, `uvicorn[standard]` 0.29 | |
| Async I/O | `httpx`, `aiohttp`, `websockets` | Never add `requests` / `urllib3` |
| Scheduling | `apscheduler` 3.10 | |
| Exchange (primary) | `binance-connector` 3.7 | Official; preferred |
| Exchange (fallback) | `python-binance` 1.0.19 | Only if connector lacks an endpoint |
| LLM | `openai` 1.30 | v1 SDK syntax |
| TA | `pandas` 2.2, `ta` 0.11, `numpy` 1.26.4 (`pandas-ta` optional) | `numpy<2` is required by `vectorbt`; `pandas-ta` 0.4 wants numpy≥2.2 → unpinned |
| Backtesting | `vectorbt` 0.26, `jesse` (separate install) | |
| Market data | `pycoingecko` 3.1; `openbb`, `cryptofeed` optional | |
| Sizing / ML | `scipy` 1.13 (Kelly), `scikit-learn` 1.5 (regime) | |
| Config | `pydantic` 2.7, `python-dotenv` 1.0 | **v2 syntax only** |

## Target architecture

Create modules on demand; keep boundaries strict.

```
app/
  agents/        # 7 trading agents, one module each, sharing a base class
  exchange/      # Binance.US wrapper — the ONLY place that talks to the exchange
  data/          # OHLCV fetchers, websocket streams, on-disk cache
  ta/            # indicator pipelines (pandas-ta first, fall back to ta)
  signals/       # cross-agent / cross-timeframe signal aggregation
  regime/        # sklearn regime classifier (bull / bear / chop / …)
  sizing/        # Kelly fraction + risk caps
  backtest/      # vectorbt + jesse adapters
  llm/           # openai prompts, caching, reasoning agents
  api/           # FastAPI routers
  scheduler/     # APScheduler jobs (data pulls, agent ticks, rebalance)
  config.py      # pydantic Settings: symbols, timeframes, risk caps, keys
  main.py        # FastAPI entrypoint
tests/           # pytest + pytest-asyncio
scripts/         # one-off backtests, data dumps, ops tools
```

### Hard rules (don't break these)

1. **Single source of truth.** The 25 symbols, the 4 timeframes (e.g. `1h / 4h / 1d / 1w`), and risk caps live in `app/config.py` as typed constants/enums. Never hardcode them inside an agent, indicator, or script.
2. **Exchange isolation.** All Binance calls go through `app/exchange/`. Agents and signals must not import `binance_connector` / `binance` directly — that's what makes paper-trading and dry-run actually work.
3. **Binance.US only.** Use `https://api.binance.us` endpoints. Symbol set, fees, and geofencing differ from `binance.com`. Don't copy `binance.com` examples blindly.
4. **No connector mixing.** Within a single module, use either `binance-connector` *or* `python-binance` — not both.
5. **Async-first I/O, sync TA.** Anything touching network/disk → `async def`. CPU-bound TA / backtest math stays sync (pandas/numpy don't benefit from async).
6. **Secrets via env only.** `.env` → `pydantic` `Settings`. Never log API keys, secrets, or full signed order payloads. `.env*` must stay gitignored.
7. **LLM off the hot path.** `openai` calls are non-deterministic and slow. Pre-compute, cache, or run them in a scheduler job — never inline inside order execution.
8. **Order placement is gated.** Every order goes through one wrapper in `app/exchange/` with a `dry_run` / paper-trading switch driven by `Settings`. Default to dry-run until explicitly turned off.
9. **Idempotency.** Set `newClientOrderId` on every order so retries don't double-fill.

## Build & run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install jesse                       # heavy, installed separately
# pip install openbb cryptofeed         # optional

cp .env.example .env                    # once .env.example exists
uvicorn app.main:app --reload           # once app/main.py exists

pytest -q                               # once tests/ exists
```

## Conventions

- **Pydantic v2** only: `model_config = ConfigDict(...)`, `field_validator`. Use `pydantic-settings` for `BaseSettings` if added later — not v1's `Config` class or `@validator`.
- **`pandas-ta`** is intentionally unpinned/optional. `0.3.14b` was yanked; `0.4.x` requires `numpy>=2.2`, which collides with `vectorbt`'s `numpy<2` pin. Code in [app/ta/indicators.py](app/ta/indicators.py) tries `pandas_ta` first and silently falls back to `ta` — keep it that way.
- **`numpy==1.26.4`** is locked for `vectorbt`. Any change breaks backtests.
- **Timeframes** are referenced by the enum in `app/config.py`, never by raw strings inside agents.
- **Tests:** `pytest` + `pytest-asyncio`. Mock at the `app/exchange/` boundary; never hit Binance.US from unit tests.
- **Logging:** stdlib `logging` with a module-level logger. No `print` in library code.

## Anti-patterns (reject these in review)

- Importing `requests`, `urllib3`, etc. → use `httpx` (sync/async) or `aiohttp` (streaming).
- Importing `binance` / `binance_connector` outside `app/exchange/`.
- Hardcoding a symbol list, a timeframe string, or a risk number inside an agent.
- Mixing `binance-connector` and `python-binance` in one module.
- Synchronous network calls inside `async def` (or async-only libs called from sync code).
- LLM calls inside the order-placement code path.
- `except Exception: pass` around order placement — fail loud, fail fast.

## See also

- [README.md](README.md) — pitch
- [requirements.txt](requirements.txt) — pinned versions (source of truth)
