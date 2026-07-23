"""Runtime health monitoring for scheduler, websocket, exchange, storage, and trade loop.

The ladder implemented here, every 60s, is:

    detect failure -> attempt recovery -> verify recovery ->
    if still failing -> escalate (emergency halt) -> keep retrying forever

...and, separately, once things are healthy again:

    N healthy cycles -> verify balances/positions/open-orders are readable
    and consistent -> resume trading -> continue monitoring

Every check and every recovery attempt is wrapped so NOTHING can raise out of
this loop. There is no supervisor that resurrects a dead asyncio.Task, so a
single uncaught exception here would silently and permanently kill the only
thing watching the bot — this loop must be the last thing standing. "Running"
is not the same as "running healthy": every iteration where any critical
check is failing, that fact is logged loudly (WARNING/CRITICAL) so a stalled
websocket/scheduler/exchange can never go unnoticed for hours just because
the process itself is still alive.

An external process-level watchdog also exists (scripts/watchdog.sh, cron
every 5 min) that restarts the whole uvicorn process if /healthz stops
responding or the last autopilot tick goes stale >20 min — this loop and that
script are two independent layers of defense.
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

_HEALTH_TASK: asyncio.Task | None = None
_SCHEDULER_REF = None

# Consecutive-failure streaks per named check, and the counterpart streak of
# fully-healthy iterations used to auto-clear an active emergency halt. Reset
# naturally on process restart — that's fine, a fresh process starts trusting.
_FAIL_STREAKS: dict[str, int] = {}
_HEALTHY_STREAK = 0
_CPU_LAST_TIMES: tuple[float, float] | None = None  # (cpu_seconds, wall_seconds)
_LAST_HEALTHY_AT: str | None = None  # ISO timestamp of the last fully-healthy iteration


def set_scheduler_ref(scheduler) -> None:
    global _SCHEDULER_REF
    _SCHEDULER_REF = scheduler


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

    scheduler_status = {
        "running": bool(_SCHEDULER_REF.running) if _SCHEDULER_REF is not None else False,
        "jobs": [j.id for j in _SCHEDULER_REF.get_jobs()] if _SCHEDULER_REF is not None else [],
    }
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


def _trigger_emergency_halt(reason: str) -> None:
    existing = storage.kv_get("emergency_halt") or {}
    if existing.get("active"):
        return  # already active — don't spam logs/kv on every iteration
    payload = {
        "active": True,
        "reason": reason,
        "since": _now_iso(),
        "auto": True,
    }
    storage.kv_set("emergency_halt", payload)
    log.critical(
        "EMERGENCY HALT ENGAGED — new BUY entries blocked (existing positions "
        "still monitored/protected by risk gates). reason=%s", reason,
    )


def _maybe_clear_emergency_halt() -> None:
    existing = storage.kv_get("emergency_halt") or {}
    if not existing.get("active"):
        return
    existing["active"] = False
    existing["cleared_at"] = _now_iso()
    storage.kv_set("emergency_halt", existing)
    log.warning(
        "EMERGENCY HALT CLEARED — all checks healthy for %d consecutive cycles; "
        "new BUY entries resumed. was: %s",
        get_settings().emergency_halt_auto_clear_cycles, existing.get("reason"),
    )


async def _verify_safe_to_resume(mode: str) -> tuple[bool, str]:
    """Before actually resuming trading after an emergency halt, explicitly
    re-verify balances, positions, and open orders are all readable and
    consistent — "verify balances / verify positions / resume trading safely"
    from the recovery ladder. A healthy-iteration COUNT alone is not proof
    it's safe to resume; this does one more real check first.
    """
    from app.trading.portfolio import portfolio_snapshot  # local import: avoid cycle

    try:
        snap = await portfolio_snapshot(mode=mode)
        if not snap:
            return False, "balance verification returned no data"
    except Exception as e:  # noqa: BLE001
        return False, f"balance verification failed: {e}"

    try:
        storage.all_positions()
    except Exception as e:  # noqa: BLE001
        return False, f"position verification failed: {e}"

    try:
        await BinanceUSClient().open_orders()
    except Exception as e:  # noqa: BLE001
        return False, f"open-order verification failed: {e}"

    return True, "balances/positions/open-orders all verified readable"


async def _health_loop() -> None:
    """Watchdog loop. MUST NEVER DIE — every check and every recovery attempt
    below is best-effort: failures are logged, NEVER raised. There is no
    `except Exception: raise` anywhere in this module (verify with
    `grep -n raise app/trading/health.py` — the only matches are this
    comment and the docstring). A bare raise here would silently kill this
    background task forever, since nothing resurrects a dead asyncio.Task —
    that is the exact "silent scheduler death" failure mode this loop exists
    to catch. The outer try/except is defense-in-depth in case a future edit
    ever reintroduces one.
    """
    global _HEALTHY_STREAK, _LAST_HEALTHY_AT
    last_wall = time.monotonic()
    while True:
        s = get_settings()
        status = {
            "timestamp": _now_iso(),
            "scheduler_alive": bool(_SCHEDULER_REF.running) if _SCHEDULER_REF is not None else False,
            "websocket_alive": bool(live_prices.connected),
            "binance_alive": False,
            "database_alive": False,
            "trade_loop_alive": False,
            "actions": [],
        }
        try:
            try:
                storage.kv_set("health_last_ping", status["timestamp"])
                _ = storage.kv_get("health_last_ping")
                status["database_alive"] = True
            except Exception as e:  # noqa: BLE001
                log.exception("health check: database ping failed: %s", e)

            # Automatic API retries: transient blips (a single dropped
            # connection, a momentary 5xx) must not immediately count as a
            # full exchange outage.
            api_start = time.monotonic()
            ok, _account = await _retry_async(
                lambda: BinanceUSClient().account(), attempts=3, base_delay=1.0, label="binance account ping",
            )
            status["binance_alive"] = ok
            if ok:
                latency = time.monotonic() - api_start
                status["binance_latency_s"] = round(latency, 3)
                if latency > s.health_latency_warn_seconds:
                    log.warning("health check: abnormal binance API latency %.2fs", latency)

            # detect failure -> attempt recovery -> verify recovery.
            if not live_prices.connected:
                recovered = await _attempt_recovery(
                    recover_fn=live_prices.start,
                    verify_fn=lambda: live_prices.connected,
                    label="websocket restart",
                )
                status["actions"].append("restarted_websocket" + ("_verified" if recovered else "_unverified"))
            else:
                stale, detail = _check_stale_price()
                status["stale_price"] = stale
                if stale:
                    log.warning("health check: %s — restarting websocket", detail)

                    async def _restart_ws():
                        await live_prices.stop()
                        live_prices.start()

                    recovered = await _attempt_recovery(
                        recover_fn=_restart_ws,
                        verify_fn=lambda: live_prices.connected,
                        label="stale-websocket restart",
                    )
                    status["actions"].append("restarted_stale_websocket" + ("_verified" if recovered else "_unverified"))

            if _SCHEDULER_REF is not None and not _SCHEDULER_REF.running:
                recovered = await _attempt_recovery(
                    recover_fn=_SCHEDULER_REF.start,
                    verify_fn=lambda: bool(_SCHEDULER_REF.running),
                    label="scheduler restart",
                )
                status["actions"].append("restarted_scheduler" + ("_verified" if recovered else "_unverified"))

            last_tick = autopilot.state.last_tick_at
            if autopilot.state.running and last_tick is not None:
                age = (datetime.now(timezone.utc) - last_tick).total_seconds()
                status["trade_loop_alive"] = age <= 1800
                if age > 1800 and _SCHEDULER_REF is not None:
                    try:
                        _SCHEDULER_REF.wakeup()
                        status["actions"].append("nudged_scheduler_wakeup")
                    except Exception as e:  # noqa: BLE001
                        log.exception("health check: scheduler wakeup failed: %s", e)
            else:
                status["trade_loop_alive"] = not autopilot.state.running

            # ── extended detection: duplicates, failed orders, open orders,
            #    portfolio discrepancy, memory, cpu ────────────────────────
            try:
                dup, dup_detail = _check_duplicate_orders()
                status["duplicate_orders"] = dup
                if dup:
                    log.critical("health check: %s", dup_detail)
            except Exception as e:  # noqa: BLE001
                log.exception("health check: duplicate-order check failed: %s", e)
                dup = False

            try:
                dup_pos, dup_pos_detail = _check_duplicate_positions()
                status["duplicate_positions"] = dup_pos
                if dup_pos:
                    log.critical("health check: %s", dup_pos_detail)
            except Exception as e:  # noqa: BLE001
                log.exception("health check: duplicate-position check failed: %s", e)
                dup_pos = False

            try:
                order_fail, order_fail_count = _check_failed_orders()
                status["order_failures"] = order_fail
                status["order_failure_count"] = order_fail_count
                if order_fail:
                    log.warning(
                        "health check: %d exchange order failures in last %dm",
                        order_fail_count, s.health_order_failure_lookback_minutes,
                    )
            except Exception as e:  # noqa: BLE001
                log.exception("health check: failed-order check failed: %s", e)
                order_fail = False

            try:
                open_orders_bad, open_orders_count = await _check_open_orders()
                status["open_orders_unexpected"] = open_orders_bad
                status["open_orders_count"] = open_orders_count
                if open_orders_bad:
                    log.warning(
                        "health check: %d unexpected resting open order(s) — "
                        "this strategy only places MARKET orders", open_orders_count,
                    )
            except Exception as e:  # noqa: BLE001
                log.exception("health check: open-orders check failed: %s", e)
                open_orders_bad = False

            try:
                mismatch, mismatch_count = await _check_portfolio_discrepancy(autopilot.state.mode)
                status["portfolio_discrepancy"] = mismatch
                status["portfolio_mismatch_count"] = mismatch_count
                if mismatch:
                    log.warning(
                        "health check: %d position(s) on file with zero exchange "
                        "balance — reconcile_portfolio job will clean up", mismatch_count,
                    )
            except Exception as e:  # noqa: BLE001
                log.exception("health check: portfolio discrepancy check failed: %s", e)

            try:
                rss_mb = _check_memory()
                status["rss_mb"] = round(rss_mb, 1)
                if rss_mb > s.health_memory_rss_warn_mb:
                    log.warning(
                        "health check: RSS %.0fMB exceeds warn threshold %.0fMB "
                        "(possible memory leak — logged only, process is never "
                        "auto-terminated)", rss_mb, s.health_memory_rss_warn_mb,
                    )
            except Exception as e:  # noqa: BLE001
                log.exception("health check: memory check failed: %s", e)

            try:
                now_wall = time.monotonic()
                cpu_pct = _check_cpu_pct(now_wall - last_wall)
                last_wall = now_wall
                status["cpu_pct"] = round(cpu_pct, 1)
                if cpu_pct > s.health_cpu_warn_pct:
                    log.warning("health check: sustained high CPU %.0f%%", cpu_pct)
            except Exception as e:  # noqa: BLE001
                log.exception("health check: cpu check failed: %s", e)

            # ── escalation ladder ──────────────────────────────────────────
            try:
                checks = {
                    "scheduler_alive": not status["scheduler_alive"],
                    "binance_alive": not status["binance_alive"],
                    "database_alive": not status["database_alive"],
                    "trade_loop_alive": not status["trade_loop_alive"],
                    "duplicate_orders": dup,
                    "duplicate_positions": dup_pos,
                    "order_failures": order_fail,
                    "open_orders_unexpected": open_orders_bad,
                }
                any_failing = False
                for name, failing in checks.items():
                    if failing:
                        any_failing = True
                        _FAIL_STREAKS[name] = _FAIL_STREAKS.get(name, 0) + 1
                        if _FAIL_STREAKS[name] >= s.emergency_halt_max_failures:
                            _trigger_emergency_halt(
                                f"{name} unhealthy for {_FAIL_STREAKS[name]} consecutive checks"
                            )
                    else:
                        _FAIL_STREAKS[name] = 0

                if any_failing:
                    _HEALTHY_STREAK = 0
                else:
                    _HEALTHY_STREAK += 1
                    if _HEALTHY_STREAK >= s.emergency_halt_auto_clear_cycles:
                        halt = storage.kv_get("emergency_halt") or {}
                        if halt.get("active"):
                            # "verify balances / verify positions / resume
                            # trading safely" — one real check, not just a
                            # streak counter, before actually resuming.
                            safe, detail = await _verify_safe_to_resume(autopilot.state.mode)
                            if safe:
                                _maybe_clear_emergency_halt()
                                log.warning("health recovery: RESUMING TRADING — %s", detail)
                            else:
                                log.critical(
                                    "health recovery: healthy streak met but resume "
                                    "verification FAILED (%s) — halt remains engaged", detail,
                                )
                                _HEALTHY_STREAK = 0

                halt_now = storage.kv_get("emergency_halt") or {}
                status["emergency_halt"] = bool(halt_now.get("active"))
                status["fail_streaks"] = dict(_FAIL_STREAKS)

                # "RUNNING" != "RUNNING HEALTHY": while any critical check is
                # down, say so loudly on every single iteration so an outage
                # can never go unnoticed just because the process is alive.
                overall_healthy = not any_failing and not status["emergency_halt"]
                status["overall_healthy"] = overall_healthy
                if overall_healthy:
                    _LAST_HEALTHY_AT = status["timestamp"]
                else:
                    failing_names = [n for n, f in checks.items() if f]
                    log.warning(
                        "HEALTH STATUS: RUNNING BUT NOT HEALTHY — failing=%s "
                        "fail_streaks=%s emergency_halt=%s last_healthy_at=%s",
                        failing_names, _FAIL_STREAKS, status["emergency_halt"], _LAST_HEALTHY_AT,
                    )
                status["last_healthy_at"] = _LAST_HEALTHY_AT
            except Exception as e:  # noqa: BLE001
                log.exception("health check: escalation ladder failed: %s", e)

            try:
                storage.kv_set("health_status", status)
            except Exception as e:  # noqa: BLE001
                log.exception("health check: failed to persist health_status: %s", e)
        except Exception as e:  # noqa: BLE001
            # Belt-and-suspenders: no single failed check may ever end this loop.
            log.exception("health loop iteration failed unexpectedly: %s", e)

        await asyncio.sleep(60)


def start_health_monitor(scheduler) -> None:
    global _HEALTH_TASK
    set_scheduler_ref(scheduler)
    if _HEALTH_TASK is not None and not _HEALTH_TASK.done():
        return
    _HEALTH_TASK = asyncio.create_task(_health_loop(), name="health-monitor")


async def stop_health_monitor() -> None:
    global _HEALTH_TASK
    if _HEALTH_TASK is None:
        return
    _HEALTH_TASK.cancel()
    try:
        await _HEALTH_TASK
    except asyncio.CancelledError:
        pass
    _HEALTH_TASK = None
