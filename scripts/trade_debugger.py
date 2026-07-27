#!/usr/bin/env python3
"""Human-readable trade decision debugger.

Prints recent trade audit rows with gate-level pass/fail details so operators can
see exactly why each symbol was executed or rejected.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

# Make `app` importable when executed as: python scripts/trade_debugger.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.storage import storage

_LAST_TICK_DEBUG_KEY = "autopilot_last_tick_debug"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect per-tick trade decisions")
    parser.add_argument("--limit", type=int, default=30, help="Number of recent audit rows (default: 30)")
    parser.add_argument(
        "--only-rejected",
        action="store_true",
        help="Show only non-submitted/rejected outcomes",
    )
    parser.add_argument(
        "--only-executed",
        action="store_true",
        help="Show only submitted/executed outcomes",
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default="",
        help="Filter by symbol, e.g. BTCUSDT",
    )
    parser.add_argument(
        "--show-last-tick",
        action="store_true",
        help="Also print in-memory per-symbol debug from the latest tick",
    )
    return parser.parse_args()


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
            if isinstance(decoded, dict):
                return decoded
        except json.JSONDecodeError:
            return {}
    return {}


def _fmt_ts(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return ts


def _outcome_status(row: dict[str, Any]) -> str:
    attempted = _to_bool(row.get("execution_attempted"))
    resp = str(row.get("binance_response") or "").upper()
    outcome = str(row.get("final_outcome") or "")
    if attempted and resp in {"SUCCESS", "FILLED", "PARTIALLY_FILLED"}:
        return "PASSED"
    if attempted and outcome.startswith("executed"):
        return "PASSED"
    return "FAILED"


def _print_header() -> None:
    s = get_settings()
    mode = "paper" if s.paper_trading else "live"
    print("=" * 80)
    print("TRADE DEBUGGER")
    print("=" * 80)
    print(f"mode={mode} live_mode={s.live_mode} paper_trading={s.paper_trading} dry_run={s.dry_run}")
    print(f"min_signal_confidence={s.min_signal_confidence:.3f} ml_gate_threshold={s.ml_gate_threshold:.3f}")
    print("=" * 80)


def _print_filters(filters: dict[str, Any]) -> None:
    if not filters:
        return
    print("filters:")
    for gate, gate_data in filters.items():
        data = gate_data if isinstance(gate_data, dict) else {"detail": str(gate_data)}
        ok = data.get("ok")
        detail = str(data.get("detail") or "")
        gate_status = "PASS" if _to_bool(ok) else "FAIL"
        print(f"  - {gate:<20} {gate_status:<4} {detail}")


def _print_audit_row(row: dict[str, Any]) -> None:
    detail = _as_dict(row.get("detail"))
    trace = _as_dict(detail.get("trace"))
    filters = _as_dict(trace.get("filters"))

    symbol = str(row.get("symbol") or "?")
    signal = str(row.get("signal") or "?")
    conf = row.get("confidence")
    threshold = None
    if "signal_confidence" in filters and isinstance(filters["signal_confidence"], dict):
        threshold = filters["signal_confidence"].get("detail")
    elif isinstance(detail.get("threshold"), (int, float)):
        threshold = f"threshold={float(detail['threshold']):.3f}"

    status = _outcome_status(row)
    response = str(row.get("binance_response") or "")
    attempted = "YES" if _to_bool(row.get("execution_attempted")) else "NO"
    outcome = str(row.get("final_outcome") or "")
    reason = str(detail.get("detail") or outcome)

    print("-" * 80)
    print(symbol)
    print(f"ts: {_fmt_ts(str(row.get('ts') or ''))}")
    print(f"signal: {signal}")
    if conf is not None:
        print(f"confidence: {float(conf):.4f}")
    if threshold:
        print(f"threshold: {threshold}")
    print(status)
    print(f"reason: {reason}")
    print(f"submitted: {attempted}")
    print(f"binance: {response or 'N/A'}")
    _print_filters(filters)


def _matches_filters(row: dict[str, Any], args: argparse.Namespace) -> bool:
    if args.symbol and str(row.get("symbol") or "").upper() != args.symbol.upper():
        return False
    attempted = _to_bool(row.get("execution_attempted"))
    if args.only_rejected and attempted:
        return False
    if args.only_executed and not attempted:
        return False
    return True


def _print_last_tick_debug() -> None:
    payload = storage.kv_get(_LAST_TICK_DEBUG_KEY) or {}
    if not payload:
        print("\nNo autopilot_last_tick_debug found in kv store.")
        return

    print("\n" + "=" * 80)
    print("LAST TICK DEBUG (KV)")
    print("=" * 80)
    print(f"ts: {payload.get('ts')}")
    print(f"total_signals: {payload.get('total_signals')}")
    print(f"by_reason: {payload.get('by_reason')}")

    per_symbol = payload.get("per_symbol") or {}
    if not isinstance(per_symbol, dict) or not per_symbol:
        print("per_symbol: <empty>")
        return

    for sym, info in per_symbol.items():
        if not isinstance(info, dict):
            continue
        print("-" * 80)
        print(sym)
        print(f"action: {info.get('action')}")
        print(f"confidence: {info.get('confidence')}")
        print(f"final_reason: {info.get('final_reason')}")
        print(f"submitted: {info.get('submitted')}")
        _print_filters(_as_dict(info.get("filters")))


def main() -> None:
    args = _parse_args()
    if args.only_rejected and args.only_executed:
        raise SystemExit("Use only one of --only-rejected or --only-executed")

    _print_header()

    rows = storage.recent_trade_audit(limit=max(1, args.limit))
    filtered = [r for r in rows if _matches_filters(r, args)]

    if not filtered:
        print("No matching trade_audit rows.")
    else:
        for row in filtered:
            _print_audit_row(row)

    if args.show_last_tick:
        _print_last_tick_debug()


if __name__ == "__main__":
    main()
