"""Read-only explanation of why the live bot can or cannot enter a BUY.

Run as the service account so APP_ENV_FILE points at the deployed env file:

    runuser -u cryptobot -- env APP_ENV_FILE=/var/lib/crypto-bot/.env \
      /opt/crypto-trading-machine/.venv/bin/python -m scripts.check_live_buy_readiness

This command never places, cancels, or edits orders/state.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.config import get_settings
from app.storage import storage


_SCAN_MAX_AGE = timedelta(minutes=5)
_SAFETY_KEYS = (
    "baseline_unavailable",
    "drawdown_halt",
    "emergency_halt",
    "tick_protection",
    "circuit_breaker_check_failed",
    "exchange_filters_unavailable",
    "ml_gate_unavailable",
)


def readiness_blockers(
    settings: Any,
    autopilot_state: dict,
    entry_status: dict,
    safety_states: dict[str, dict],
    scan: dict,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Return current live-BUY blockers from settings and persisted telemetry."""
    blockers: list[str] = []
    if not settings.live_mode:
        blockers.append("LIVE_MODE is false")
    if settings.paper_trading:
        blockers.append("PAPER_TRADING is true")
    if settings.dry_run:
        blockers.append("DRY_RUN is true")
    if not settings.live_buys_enabled:
        blockers.append("LIVE_BUYS_ENABLED is false")
    if not autopilot_state.get("running"):
        blockers.append("autopilot is not running")
    if autopilot_state.get("mode") != "live":
        blockers.append(f"autopilot mode is {autopilot_state.get('mode')!r}, not live")
    if not isinstance(entry_status, dict):
        blockers.append("entry status is unavailable")
    elif entry_status.get("entry_halted"):
        blockers.append("entry status is halted: " + ", ".join(entry_status.get("reasons") or []))

    for key in _SAFETY_KEYS:
        state = safety_states.get(key)
        if not isinstance(state, dict):
            blockers.append(f"safety state {key} is unavailable")
        elif state.get("active"):
            blockers.append(f"{key} is active: {state.get('reason', 'no reason recorded')}")

    if not scan:
        blockers.append("no persisted ProfitStream scan")
        return blockers

    raw_timestamp = scan.get("timestamp")
    try:
        scan_time = datetime.fromisoformat(str(raw_timestamp))
        if scan_time.tzinfo is None:
            scan_time = scan_time.replace(tzinfo=timezone.utc)
        current_time = now or datetime.now(timezone.utc)
        if current_time - scan_time > _SCAN_MAX_AGE:
            blockers.append(f"ProfitStream scan is stale ({raw_timestamp})")
    except (TypeError, ValueError):
        blockers.append("ProfitStream scan timestamp is missing or invalid")

    actions = scan.get("actions") or {}
    buy_actions = int(actions.get("BUY", 0) or 0)
    qualified_buy_candidates = scan.get("qualified_buy_candidates")
    threshold = int(scan.get("score_threshold", settings.profitstream_score_threshold))
    highest_score = int(scan.get("highest_score", 0) or 0)
    if buy_actions == 0:
        blockers.append("latest ProfitStream scan produced no BUY actions")
    if qualified_buy_candidates is None:
        blockers.append("latest scan does not record score-qualified BUY candidates")
    elif int(qualified_buy_candidates) == 0:
        blockers.append("no BUY candidate passed the runner score threshold")
    if highest_score < threshold:
        blockers.append(f"highest scan score {highest_score} is below threshold {threshold}")
    if settings.market_regime_gate_enabled and scan.get("market_allowed") is not True:
        blockers.append("BTC market-regime gate is not allowing entries")
    if settings.breadth_gate_enabled:
        breadth = scan.get("breadth")
        if not isinstance(breadth, dict) or breadth.get("allowed") is not True:
            blockers.append("alt-market breadth gate is not allowing entries")
    return blockers


def main() -> int:
    settings = get_settings()
    autopilot_state = storage.kv_get("autopilot_state") or {}
    entry_status = storage.kv_get("entry_status") or {}
    safety_states = {key: storage.kv_get(key) or {} for key in _SAFETY_KEYS}
    scan = storage.kv_get("profitstream_scan") or {}
    blockers = readiness_blockers(
        settings, autopilot_state, entry_status, safety_states, scan
    )

    print("Live BUY readiness: " + ("READY FOR AN ELIGIBLE SIGNAL" if not blockers else "BLOCKED"))
    print(
        f"mode=live_mode:{settings.live_mode} paper:{settings.paper_trading} "
        f"dry_run:{settings.dry_run} live_buys_enabled:{settings.live_buys_enabled}"
    )
    if scan:
        print(
            "latest_scan="
            f"{scan.get('timestamp')} BTC={scan.get('btc_regime_label')} "
            f"breadth={scan.get('breadth')} actions={scan.get('actions')} "
            f"highest_score={scan.get('highest_score')} threshold={scan.get('score_threshold')}"
        )
    for blocker in blockers:
        print("BLOCKER: " + blocker)
    print("This is a read-only readiness check; READY does not guarantee a signal or fill.")
    return 0 if not blockers else 1


if __name__ == "__main__":
    raise SystemExit(main())