"""Runtime health monitoring for scheduler, websocket, exchange, storage, and trade loop.

Also owns the auto-recovery escalation ladder: each check below is retried
inline (restart websocket / restart scheduler) every loop iteration. If a
critical check stays unhealthy for `emergency_halt_max_failures` consecutive
iterations despite recovery attempts, `_trigger_emergency_halt` sets a
persisted flag that `Autopilot.tick()` consults to stop opening NEW positions
(same mechanism as the drawdown circuit breaker) while leaving risk-gate
exits, monitoring, and the process itself completely untouched. The halt
auto-clears once every check has been healthy for
`emergency_halt_auto_clear_cycles` consecutive iterations.
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


def set_scheduler_ref(scheduler) -> None:
    global _SCHEDULER_REF
    _SCHEDULER_REF = scheduler


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    try:
        client = BinanceUSClient()
        open_orders = await client.open_orders()
        return len(open_orders) > 0, len(open_orders)
    except Exception as e:  # noqa: BLE001
        log.debug("health check: open_orders fetch failed (non-fatal): %s", e)
        return False, 0


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


async def _health_loop() -> None:
    """Watchdog loop. MUST NEVER DIE — every check below is best-effort and
    failures are logged, not raised. A `raise` here would silently kill this
    background task forever (no supervisor resurrects an asyncio.Task), which
    is the exact "silent scheduler death" failure mode this loop exists to
    catch. The outer try/except is defense-in-depth in case a future edit
    reintroduces a bare raise inside one of the checks.
    """
    global _HEALTHY_STREAK
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

            api_start = time.monotonic()
            try:
                _ = await BinanceUSClient().account()
                status["binance_alive"] = True
                latency = time.monotonic() - api_start
                status["binance_latency_s"] = round(latency, 3)
                if latency > s.health_latency_warn_seconds:
                    log.warning("health check: abnormal binance API latency %.2fs", latency)
            except Exception as e:  # noqa: BLE001
                log.exception("health check: binance ping failed: %s", e)

            if not live_prices.connected:
                try:
                    live_prices.start()
                    status["actions"].append("restarted_websocket")
                except Exception as e:  # noqa: BLE001
                    log.exception("health check: websocket restart failed: %s", e)
            else:
                stale, detail = _check_stale_price()
                status["stale_price"] = stale
                if stale:
                    log.warning("health check: %s — restarting websocket", detail)
                    try:
                        await live_prices.stop()
                        live_prices.start()
                        status["actions"].append("restarted_stale_websocket")
                    except Exception as e:  # noqa: BLE001
                        log.exception("health check: stale-websocket restart failed: %s", e)

            if _SCHEDULER_REF is not None and not _SCHEDULER_REF.running:
                try:
                    _SCHEDULER_REF.start()
                    status["actions"].append("restarted_scheduler")
                except Exception as e:  # noqa: BLE001
                    log.exception("health check: scheduler restart failed: %s", e)

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
                        _maybe_clear_emergency_halt()

                status["emergency_halt"] = bool((storage.kv_get("emergency_halt") or {}).get("active"))
                status["fail_streaks"] = dict(_FAIL_STREAKS)
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
