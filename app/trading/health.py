"""Runtime health monitoring for scheduler, websocket, exchange, storage, and trade loop."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.exchange import BinanceUSClient
from app.exchange.ws_stream import live_prices
from app.logging_setup import get_logger
from app.storage import storage
from app.trading.autopilot import autopilot

log = get_logger(__name__)

_HEALTH_TASK: asyncio.Task | None = None
_SCHEDULER_REF = None


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


async def _health_loop() -> None:
    """Watchdog loop. MUST NEVER DIE — every check below is best-effort and
    failures are logged, not raised. A `raise` here would silently kill this
    background task forever (no supervisor resurrects an asyncio.Task), which
    is the exact "silent scheduler death" failure mode this loop exists to
    catch. The outer try/except is defense-in-depth in case a future edit
    reintroduces a bare raise inside one of the checks.
    """
    while True:
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

            try:
                _ = await BinanceUSClient().account()
                status["binance_alive"] = True
            except Exception as e:  # noqa: BLE001
                log.exception("health check: binance ping failed: %s", e)

            if not live_prices.connected:
                try:
                    live_prices.start()
                    status["actions"].append("restarted_websocket")
                except Exception as e:  # noqa: BLE001
                    log.exception("health check: websocket restart failed: %s", e)

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
