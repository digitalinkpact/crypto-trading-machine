"""WATCHDOG ENGINE — the background supervisor that owns the health-check
loop and the emergency-halt escalation ladder.

This module is intentionally separate from `app/trading/health.py` (which
only holds the individual, unit-testable *checks*): this is the orchestrator
that runs those checks every 60s, attempts recovery, verifies it worked, and
escalates when it doesn't. As the codebase grows (strategy.py, risk.py,
portfolio.py, autopilot.py, health.py, watchdog.py, scheduler/, analytics...)
each file should do exactly one job — this one supervises the others.

The ladder, every 60s:

    detect failure -> attempt recovery -> verify recovery ->
    if still failing -> escalate (emergency halt) -> keep retrying forever

...and, separately, once things are healthy again:

    N healthy cycles -> verify balances/positions/open-orders are readable
    and consistent -> resume trading -> continue monitoring

EMERGENCY HALT LEVELS (persisted in the `emergency_halt` kv row as `level`):

    new_entries_blocked   — N consecutive failures of a health check (scheduler
                             /websocket/exchange/database/trade-loop/duplicate
                             -order/duplicate-position/failed-order). New BUY
                             entries are refused; existing positions are still
                             monitored and protected by the risk-gate loop.
    order_outcome_unknown — a live order placement raised an exception and
                             Autopilot._resolve_order_after_exception() could
                             NOT prove whether Binance received/filled it (see
                             app/trading/autopilot.py). Same blocking effect as
                             above, but flagged distinctly since the cause is
                             an unverifiable order rather than a health check.

Both levels clear the same way: `emergency_halt_auto_clear_cycles` consecutive
fully-healthy iterations, THEN one real verify-balances/positions/open-orders
check (`_verify_safe_to_resume`) before actually resuming — a healthy streak
COUNT alone is never trusted as proof it's safe.

Deliberately NOT implemented, by explicit design decision (see repo memory):
automatically switching `positions.mode` from "live" to "paper" on failure —
that would strand real open exchange positions without stop-loss/take-profit
coverage, since the risk-gate loop keys off `mode`. Automatically resubmitting
a failed/unknown order is also deliberately NOT implemented — an unverifiable
order must never risk a duplicate fill; missing a trade is always the safer
outcome and is explicitly acceptable.

Every check and every recovery attempt is wrapped so NOTHING can raise out of
this loop. There is no supervisor that resurrects a dead asyncio.Task, so a
single uncaught exception here would silently and permanently kill the only
thing watching the bot — this loop must be the last thing standing. "Running"
is not the same as "running healthy": every iteration where any critical
check is failing, that fact is logged loudly (WARNING/CRITICAL) so a stalled
websocket/scheduler/exchange can never go unnoticed for hours just because
the process itself is still alive. There is no `except Exception: raise`
anywhere in this module — verify with `grep -n raise app/trading/watchdog.py`
(the only matches are this comment and the docstrings below).

An external process-level watchdog also exists (scripts/watchdog.sh, cron
every 5 min) that restarts the whole uvicorn process if /healthz stops
responding or the last autopilot tick goes stale >20 min — this loop and that
script are two independent layers of defense.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from app.config import get_settings
from app.exchange import BinanceUSClient
from app.exchange.ws_stream import live_prices
from app.logging_setup import get_logger
from app.storage import storage
from app.trading import health
from app.trading.autopilot import autopilot

log = get_logger(__name__)

_HEALTH_TASK: asyncio.Task | None = None
_SCHEDULER_REF = None

# Consecutive-failure streaks per named check, and the counterpart streak of
# fully-healthy iterations used to auto-clear an active emergency halt. Reset
# naturally on process restart — that's fine, a fresh process starts trusting.
_FAIL_STREAKS: dict[str, int] = {}
_HEALTHY_STREAK = 0
_LAST_HEALTHY_AT: str | None = None  # ISO timestamp of the last fully-healthy iteration


def set_scheduler_ref(scheduler) -> None:
    global _SCHEDULER_REF
    _SCHEDULER_REF = scheduler


def get_scheduler_status() -> dict:
    """Read-only scheduler status for startup_report() etc. — kept here since
    watchdog.py owns the scheduler reference."""
    return {
        "running": bool(_SCHEDULER_REF.running) if _SCHEDULER_REF is not None else False,
        "jobs": [j.id for j in _SCHEDULER_REF.get_jobs()] if _SCHEDULER_REF is not None else [],
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def trigger_emergency_halt(reason: str, *, level: str = "new_entries_blocked") -> None:
    """Engage the emergency halt (idempotent — a second call while already
    active does not overwrite the original reason/since/level). Public so
    other modules (Autopilot's order-outcome-unknown protocol) can escalate
    into the same halt mechanism the watchdog loop uses, instead of each
    inventing its own blocking flag.
    """
    existing = storage.kv_get("emergency_halt") or {}
    if existing.get("active"):
        return  # already active — don't spam logs/kv on every iteration
    payload = {
        "active": True,
        "level": level,
        "reason": reason,
        "since": _now_iso(),
        "auto": True,
    }
    storage.kv_set("emergency_halt", payload)
    log.critical(
        "EMERGENCY HALT ENGAGED (level=%s) — new BUY entries blocked (existing "
        "positions still monitored/protected by risk gates). reason=%s",
        level, reason,
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
        "new BUY entries resumed. was (level=%s): %s",
        get_settings().emergency_halt_auto_clear_cycles,
        existing.get("level"), existing.get("reason"),
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
    `grep -n raise app/trading/watchdog.py` — the only matches are this
    comment and the docstrings). A bare raise here would silently kill this
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
            ok, _account = await health._retry_async(
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
                recovered = await health._attempt_recovery(
                    recover_fn=live_prices.start,
                    verify_fn=lambda: live_prices.connected,
                    label="websocket restart",
                )
                status["actions"].append("restarted_websocket" + ("_verified" if recovered else "_unverified"))
            else:
                stale, detail = health._check_stale_price()
                status["stale_price"] = stale
                if stale:
                    log.warning("health check: %s — restarting websocket", detail)

                    async def _restart_ws():
                        await live_prices.stop()
                        live_prices.start()

                    recovered = await health._attempt_recovery(
                        recover_fn=_restart_ws,
                        verify_fn=lambda: live_prices.connected,
                        label="stale-websocket restart",
                    )
                    status["actions"].append("restarted_stale_websocket" + ("_verified" if recovered else "_unverified"))

            if _SCHEDULER_REF is not None and not _SCHEDULER_REF.running:
                recovered = await health._attempt_recovery(
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
                dup, dup_detail = health._check_duplicate_orders()
                status["duplicate_orders"] = dup
                if dup:
                    log.critical("health check: %s", dup_detail)
            except Exception as e:  # noqa: BLE001
                log.exception("health check: duplicate-order check failed: %s", e)
                dup = False

            try:
                dup_pos, dup_pos_detail = health._check_duplicate_positions()
                status["duplicate_positions"] = dup_pos
                if dup_pos:
                    log.critical("health check: %s", dup_pos_detail)
            except Exception as e:  # noqa: BLE001
                log.exception("health check: duplicate-position check failed: %s", e)
                dup_pos = False

            try:
                order_fail, order_fail_count = health._check_failed_orders()
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
                open_orders_bad, open_orders_count = await health._check_open_orders()
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
                mismatch, mismatch_count = await health._check_portfolio_discrepancy(autopilot.state.mode)
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
                rss_mb = health._check_memory()
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
                cpu_pct = health._check_cpu_pct(now_wall - last_wall)
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
                            trigger_emergency_halt(
                                f"{name} unhealthy for {_FAIL_STREAKS[name]} consecutive checks",
                                level="new_entries_blocked",
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
                status["emergency_halt_level"] = halt_now.get("level")
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
                        "fail_streaks=%s emergency_halt=%s (level=%s) last_healthy_at=%s",
                        failing_names, _FAIL_STREAKS, status["emergency_halt"],
                        status["emergency_halt_level"], _LAST_HEALTHY_AT,
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
