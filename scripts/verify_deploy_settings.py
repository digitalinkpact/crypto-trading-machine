"""Print the EFFECTIVE win-rate guardrail settings and fail on any widening.

Run on the droplet from the repo root (used by scripts/verify_deploy.sh):

    .venv/bin/python -m scripts.verify_deploy_settings

Values are read exactly as the app loads them (.env + process environment), then
compared with the evidence-locked limits from the 2026-09-29 win-rate
regression. Exit 0 = all guardrails hold, 1 = at least one is widened/loosened.
"""
from __future__ import annotations

import sys

from app.config import Settings, get_settings

# (setting, comparison, locked value). "<=" and ">=" are the safe directions.
GUARDRAILS: tuple[tuple[str, str, object], ...] = (
    ("max_open_positions", "<=", 12),
    ("aggressive_max_open_positions", "<=", 12),
    ("rollback_max_open_positions", "<=", 12),
    ("universe_size", "<=", 75),
    ("final_pairlist_size", "<=", 50),
    ("stop_loss_pct", ">=", 0.04),
    ("atr_stop_min_pct", ">=", 0.04),
    ("profitstream_score_threshold", ">=", 80),
    ("win_rate_position_level", "==", True),
    ("mean_reversion_exit_enabled", "==", False),
    ("trend_filter_enabled", "==", True),
    ("trend_gate_bypass_enabled", "==", False),
    ("profitstream_trend_soft_penalty", "==", False),
    ("btc_alignment_hard_gate", "==", True),
    ("pullback_require_candidate_confirmation", "==", True),
    ("market_regime_gate_enabled", "==", True),
    ("max_spread_pct", "<=", 0.0025),
    ("aggressive_max_spread_pct", "<=", 0.0025),
)


def _holds(value: object, op: str, locked: object) -> bool:
    if op == "<=":
        return value <= locked  # type: ignore[operator]
    if op == ">=":
        return value >= locked  # type: ignore[operator]
    return value == locked


def main() -> int:
    s = get_settings()
    failures = 0
    print(f"{'setting':<42} {'effective':>12} {'locked':>12} {'code default':>13}  status")
    for name, op, locked in GUARDRAILS:
        value = getattr(s, name)
        default = Settings.model_fields[name].default
        if not _holds(value, op, locked):
            status, failures = "FAIL (widened)", failures + 1
        elif value != default:
            status = "ok (env override, still within lock)"
        else:
            status = "ok"
        print(f"{name:<42} {value!s:>12} {op + ' ' + str(locked):>12} {default!s:>13}  {status}")
    print(
        f"\nmode: live_mode={s.live_mode} paper_trading={s.paper_trading} dry_run={s.dry_run} "
        f"live_buys_enabled={s.live_buys_enabled}"
    )
    print("RESULT:", "ALL GUARDRAILS HOLD" if not failures else f"{failures} GUARDRAIL(S) WIDENED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
