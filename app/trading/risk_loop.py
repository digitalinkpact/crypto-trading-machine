"""Independent risk-management loop — decoupled from the strategy tick.

Stop-loss / take-profit / trailing-stop protection used to run once a minute,
nested inside `Autopilot.tick()`, gated behind the APScheduler cron AND the
asyncio event loop of the main process being alive and unblocked. A stalled
scheduler, a slow/hung agent pass, or the whole process simply not running
(the 2026-07-23→27 outage — no supervisor, no automatic restart) meant open
positions sat completely unprotected for hours to days at a time, which is how
a single position lost -40% before being force-closed.

This module runs `Autopilot._run_risk_gates()` on its own short cadence
(`settings.risk_manager_loop_seconds`, default 15s), independent of the
strategy tick, the scheduler, and — when run as its own OS process, see
deploy/systemd/crypto-bot-risk.service — even the main FastAPI process.

Two supported deployments, controlled by `settings.risk_loop_in_process_enabled`:
  - In-process (default): started as a plain asyncio task inside the same
    uvicorn process via `start()`/`stop()` from app/main.py's lifespan. Simple,
    zero extra ops, but still dies if the whole process dies.
  - Standalone process: run `python -m app.trading.risk_loop` under its own
    systemd unit (crypto-bot-risk.service, Restart=always) so risk exits keep
    firing even if the main app process is down/restarting/OOM-killed. Set
    `risk_loop_in_process_enabled=False` in that case to avoid running twice.

Either way, a cross-process kv-backed mutex (`storage.try_acquire_lock`)
guards each iteration so the two deployments can never race each other into
double-submitting an exit order for the same position.
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid

from app.config import get_settings
from app.logging_setup import get_logger
from app.storage import storage
from app.exchange.telemetry import exchange_telemetry
from app.trading.autopilot import autopilot

log = get_logger(__name__)

_LOCK_NAME = "risk_loop"
_HEARTBEAT_KEY = "risk_loop_last_run"
_OWNER = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"

_task: "asyncio.Task | None" = None
_stop_event: "asyncio.Event | None" = None


async def _run_once() -> None:
    started = time.perf_counter()
    ttl = max(10.0, float(get_settings().risk_manager_loop_seconds) * 3)
    if not storage.try_acquire_lock(_LOCK_NAME, ttl_seconds=ttl, owner=_OWNER):
        # Another risk-loop instance (in-process or standalone) already ran
        # this cycle — expected transiently during a restart/redeploy when
        # both deployments briefly overlap.
        return
    try:
        await autopilot._run_risk_gates()
        storage.kv_set(_HEARTBEAT_KEY, time.time())
    except Exception as exc:  # noqa: BLE001
        log.exception("independent risk loop iteration failed: %s", exc)
    finally:
        storage.release_lock(_LOCK_NAME, owner=_OWNER)
        exchange_telemetry.record_stage("risk_loop", time.perf_counter() - started)


async def _loop() -> None:
    assert _stop_event is not None
    log.info("independent risk loop started (owner=%s)", _OWNER)
    while not _stop_event.is_set():
        interval = max(5, int(get_settings().risk_manager_loop_seconds))
        started = time.monotonic()
        await _run_once()
        elapsed = time.monotonic() - started
        remaining = max(0.0, interval - elapsed)
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=remaining or 0.001)
        except asyncio.TimeoutError:
            pass
    log.info("independent risk loop stopped (owner=%s)", _OWNER)


def start() -> None:
    """Start the risk loop as a background asyncio task in THIS process.

    Called from app/main.py's FastAPI lifespan. No-op if already running or
    if `risk_loop_in_process_enabled` is False (standalone-process deployment
    handles it instead).
    """
    global _task, _stop_event
    if not get_settings().risk_loop_in_process_enabled:
        log.info(
            "in-process risk loop disabled (risk_loop_in_process_enabled=false) — "
            "expecting a standalone crypto-bot-risk process instead"
        )
        return
    if _task is not None and not _task.done():
        return
    _stop_event = asyncio.Event()
    _task = asyncio.create_task(_loop(), name="risk_loop")


async def stop() -> None:
    """Signal the loop to stop and wait for the current iteration to finish."""
    global _task, _stop_event
    if _stop_event is not None:
        _stop_event.set()
    if _task is not None:
        try:
            await asyncio.wait_for(_task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _task.cancel()
        _task = None


async def _standalone_main() -> None:
    """Entrypoint for running the risk loop as its own OS process:

        python -m app.trading.risk_loop

    Intended for deploy/systemd/crypto-bot-risk.service.
    """
    global _stop_event
    _stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        import signal
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _stop_event.set)
    except (NotImplementedError, RuntimeError):
        pass  # signal handlers unsupported on this platform — rely on KeyboardInterrupt
    await _loop()


if __name__ == "__main__":
    asyncio.run(_standalone_main())
