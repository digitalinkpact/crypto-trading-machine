"""Health *checks* library — pure, best-effort, individually-testable probes
for scheduler/websocket/exchange/storage/trade-loop/order/portfolio state,
plus the two generic recovery primitives (`_retry_async`, `_attempt_recovery`)
built on top of them.

This module does NOT own a background loop or the emergency-halt escalation
ladder — that orchestration lives in `app/trading/watchdog.py`, which imports
the checks from here. Splitting it this way keeps each check unit-testable
in isolation (see tests/test_health_monitor.py) while the loop/ladder itself
has its own focused tests (tests/test_watchdog.py).

Every function here is best-effort: failures are logged, NEVER raised, so a
single flaky check can never take down the caller's loop.
"""
from __future__ import annotations

import asyncio
import resource
import time
from datetime import datetime, timedelta, timezone

from app.config import get_settings
from app.exchange import BinanceUSClient
from app.exchange.ws_stream import live_prices
from app.logging_setup import get_logger
from app.storage import storage
from app.trading.autopilot import autopilot

log = get_logger(__name__)

_CPU_LAST_TIMES: tuple[float, float] | None = None  # (cpu_seconds, wall_seconds)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _retry_async(coro_fn, *, attempts: int = 3, base_delay: float = 1.0, label: str = ""):
    """Best-effort retry with linear backoff for a flaky external call (the
    "automatic API retries" requirement). NEVER raises — after the last
    attempt fails it logs an error and returns (False, None) so the caller
    can decide how to react (mark unhealthy, attempt a bigger recovery, etc).
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = await coro_fn()
            return True, result
        except Exception as e:  # noqa: BLE001
            last_exc = e
            log.warning("health check: %s attempt %d/%d failed: %s", label, attempt, attempts, e)
            if attempt < attempts:
                await asyncio.sleep(base_delay * attempt)
    log.error("health check: %s failed after %d attempts: %s", label, attempts, last_exc)
    return False, None


async def _attempt_recovery(*, recover_fn, verify_fn, label: str, verify_delay: float = 2.0) -> bool:
    """Run a recovery action, wait briefly, then re-check the thing it was
    supposed to fix — the WATCHDOG's "verify recovery" step. Recovery must
    never happen silently: the outcome (fixed or still broken) is always
    logged loudly so a failed restart is never mistaken for a successful one.
    Never raises.
    """
    try:
        result = recover_fn()
        if asyncio.iscoroutine(result):
            await result
    except Exception as e:  # noqa: BLE001
        log.error("health recovery: %s recovery attempt raised: %s", label, e)
        return False
    await asyncio.sleep(verify_delay)
    try:
        recovered = bool(verify_fn())
    except Exception as e:  # noqa: BLE001
        log.error("health recovery: %s post-recovery verification failed: %s", label, e)
        return False
    if recovered:
        log.warning("health recovery: %s recovery VERIFIED successful", label)
    else:
        log.critical(
            "health recovery: %s recovery attempt did NOT fix the problem — "
            "will retry next cycle", label,
        )
    return recovered


async def startup_report() -> dict:
    mode = "paper" if autopilot.state.mode == "paper" else "live"
    env = {
        "PAPER_TRADING": bool(getattr(__import__("app.config", fromlist=["get_settings"]).get_settings(), "paper_trading")),
        "LIVE_MODE": bool(getattr(__import__("app.config", fromlist=["get_settings"]).get_settings(), "live_mode")),
        "DRY_RUN": bool(getattr(__import__("app.config", fromlist=["get_settings"]).get_settings(), "dry_run")),
    }

    exchange_connected = False
    api_permissions = {"canTrade": None, "accountType": None}
    balances = {}
    try:
        client = BinanceUSClient()
        account = await client.account()
        exchange_connected = True
        api_permissions = {
            "canTrade": account.get("canTrade"),
            "accountType": account.get("accountType"),
        }
        balances = {
            b["asset"]: b["free"] for b in account.get("balances", []) if float(b.get("free", 0)) > 0
        }
    except Exception as e:  # noqa: BLE001
        logger = log
        logger.exception(f"Trade execution failure: {e}")

    scheduler_status = {}
    try:
        from app.trading import watchdog  # local import: avoid cycle

        scheduler_status = watchdog.get_scheduler_status()
    except Exception as e:  # noqa: BLE001
        log.debug("startup report: scheduler status unavailable: %s", e)
    ws_status = live_prices.status()

    report = {
        "timestamp": _now_iso(),
        "trading_mode": mode,
        "env": env,
        "exchange_connected": exchange_connected,
        "api_permissions": api_permissions,
        "account_balances": balances,
        "scheduler": scheduler_status,
        "websocket": ws_status,
    }
    storage.kv_set("startup_report", report)
    log.info("startup report: %s", report)
    return report


# ── individual detection checks (each best-effort, never raises) ──────────


def _check_stale_price() -> tuple[bool, str]:
    """True (unhealthy) if the websocket claims 'connected' but hasn't pushed
    a message in a long time — a hung-but-not-disconnected socket."""
    s = get_settings()
    st = live_prices.status()
    age = st.get("last_msg_age_s")
    if st.get("connected") and age is not None and age > (s.live_price_max_age_seconds * 3):
        return True, f"websocket connected but stale (last msg {age:.0f}s ago)"
    return False, ""


def _check_duplicate_orders() -> tuple[bool, str]:
    """Detect two orders for the same symbol/side/mode within a short window —
    the cross-process tick lock should make this impossible; a regression
    canary for that class of bug."""
    s = get_settings()
    window = timedelta(seconds=s.health_duplicate_order_window_seconds)
    try:
        orders = storage.recent_orders(limit=50)
    except Exception as e:  # noqa: BLE001
        log.exception("health check: recent_orders fetch failed: %s", e)
        return False, ""
    seen: dict[tuple[str, str, str], datetime] = {}
    for o in orders:
        try:
            ts = datetime.fromisoformat(str(o["ts"]))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue
        key = (o.get("mode", ""), o.get("symbol", ""), o.get("side", ""))
        prev = seen.get(key)
        if prev is not None and abs((prev - ts).total_seconds()) <= window.total_seconds():
            return True, f"duplicate order candidate: {key} within {window.total_seconds():.0f}s"
        seen[key] = ts
    return False, ""


def _check_failed_orders() -> tuple[bool, int]:
    """Count recent exchange-level order failures (submitted, rejected/not
    filled) — excludes ordinary gate-rejections, which aren't failures."""
    s = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=s.health_order_failure_lookback_minutes)
    try:
        rows = storage.recent_trade_audit(limit=300)
    except Exception as e:  # noqa: BLE001
        log.exception("health check: recent_trade_audit fetch failed: %s", e)
        return False, 0
    count = 0
    for r in rows:
        outcome = str(r.get("final_outcome") or "")
        if not outcome.startswith("rejected: Binance") and not outcome.startswith("rejected: exception"):
            continue
        try:
            ts = datetime.fromisoformat(str(r.get("ts")))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if ts >= cutoff:
            count += 1
    return count >= s.health_order_failure_max, count


async def _check_open_orders() -> tuple[bool, int]:
    """This strategy only places MARKET orders, so any resting open order is
    unexpected — could indicate a stuck/partially-processed order."""
    ok, open_orders = await _retry_async(
        lambda: BinanceUSClient().open_orders(), attempts=2, base_delay=1.0, label="open_orders fetch",
    )
    if not ok:
        return False, 0
    return len(open_orders) > 0, len(open_orders)


def _check_duplicate_positions() -> tuple[bool, str]:
    """Schema-bypass canary: the `positions` table PK is (symbol, mode), so
    two open rows for the same symbol+mode should be structurally impossible
    via the normal open_position()/upsert path. If it ever happens anyway
    (manual DB edit, a future migration bug, a new code path that bypasses
    the upsert), treat it as critical — this is exactly the duplicate-position
    class of bug that has bitten this project before (see duplicate-order
    fix, 2026-06-13)."""
    try:
        positions = storage.all_positions()
    except Exception as e:  # noqa: BLE001
        log.exception("health check: all_positions fetch failed: %s", e)
        return False, ""
    seen: dict[tuple[str, str], int] = {}
    for p in positions:
        key = (str(p.get("mode")), str(p.get("symbol")))
        seen[key] = seen.get(key, 0) + 1
    dupes = [k for k, c in seen.items() if c > 1]
    if dupes:
        return True, f"duplicate position rows detected: {dupes}"
    return False, ""


def _check_memory() -> float:
    """RSS in MB. Log-only — never used to kill the process."""
    try:
        rss_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return rss_kb / 1024.0  # Linux reports ru_maxrss in KB
    except Exception as e:  # noqa: BLE001
        log.debug("health check: memory read failed: %s", e)
        return 0.0


def _check_cpu_pct(wall_elapsed_s: float) -> float:
    """Estimate process CPU% over the last loop interval. Log-only."""
    global _CPU_LAST_TIMES
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        cpu_now = usage.ru_utime + usage.ru_stime
    except Exception as e:  # noqa: BLE001
        log.debug("health check: cpu read failed: %s", e)
        return 0.0
    pct = 0.0
    if _CPU_LAST_TIMES is not None and wall_elapsed_s > 0:
        prev_cpu, _prev_wall = _CPU_LAST_TIMES
        pct = max(0.0, (cpu_now - prev_cpu) / wall_elapsed_s * 100.0)
    _CPU_LAST_TIMES = (cpu_now, time.monotonic())
    return pct


async def _check_portfolio_discrepancy(mode: str) -> tuple[bool, int]:
    """Compare stored open positions against real exchange/paper balances.
    A mismatch (position on file the exchange no longer holds) is a
    discrepancy — the 5-min `reconcile_portfolio` job fixes these; this check
    just surfaces the count for the escalation ladder and dashboards."""
    from app.trading.portfolio import portfolio_snapshot  # local import: avoid cycle

    try:
        positions = [p for p in storage.all_positions() if p["mode"] == mode]
        if not positions:
            return False, 0
        snap = await portfolio_snapshot(mode=mode)
        balances = {k: float(v) for k, v in (snap.get("all_balances") or {}).items()}
    except Exception as e:  # noqa: BLE001
        log.debug("health check: portfolio discrepancy check failed (non-fatal): %s", e)
        return False, 0
    mismatches = 0
    for pos in positions:
        base = str(pos["symbol"]).removesuffix("USDT")
        if balances.get(base, 0.0) <= 0:
            mismatches += 1
    return mismatches > 0, mismatches

