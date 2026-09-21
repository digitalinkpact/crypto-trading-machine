"""Stage 5 — Weekly experiment verdict card (READ-ONLY).

Consolidates the outputs of Stages 1-4 into a single advisory verdict with a
ranked concerns list and prioritized human actions. Never applies a change.

The decision logic is committed up front in
``docs/research/verdict_card.md``. This module evaluates that logic against
already-computed stage outputs; it performs no I/O and imports no order path.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Type-only imports (avoid runtime cycles / heavy deps).
try:
    from app.trading.experiment_stats import PromotionVerdict
    from app.research.drift import DriftReport
    from app.research.execution_quality import SpreadCosts
except Exception:  # pragma: no cover - keep the module importable in isolation
    PromotionVerdict = Any  # type: ignore[assignment]
    DriftReport = Any  # type: ignore[assignment]
    SpreadCosts = Any  # type: ignore[assignment]


PROMOTE = "PROMOTE"
KEEP_TESTING = "KEEP_TESTING"
REJECT = "REJECT"
PAUSE = "PAUSE"


@dataclass
class VerdictCard:
    overall: str  # PROMOTE | KEEP_TESTING | REJECT | PAUSE
    reasons: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    inputs_present: dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": self.overall,
            "reasons": list(self.reasons),
            "concerns": list(self.concerns),
            "recommendations": list(self.recommendations),
            "inputs_present": dict(self.inputs_present),
        }


def _has_material_execution_drift(drift: Optional[DriftReport]) -> bool:
    if drift is None:
        return False
    return any(getattr(m, "kind", "") == "execution" and getattr(m, "material", False)
               for m in getattr(drift, "metrics", []) or [])


def _has_material_drawdown_drift(drift: Optional[DriftReport]) -> bool:
    if drift is None:
        return False
    for m in getattr(drift, "metrics", []) or []:
        if getattr(m, "name", "") == "max_drawdown_pct" and getattr(m, "material", False):
            delta = getattr(m, "delta", None)
            if delta is not None and delta > 0:
                return True
    return False


def _stage1_concerns(stage1_summary: Optional[list[dict]]) -> list[str]:
    if not stage1_summary:
        return []
    lines: list[str] = []
    for row in stage1_summary:
        reason = row.get("reason") or row.get("name") or "unknown"
        opportunity_cost = row.get("opportunity_cost_pct")
        if opportunity_cost is None:
            continue
        try:
            oc = float(opportunity_cost)
        except (TypeError, ValueError):
            continue
        if oc > 0.005:
            lines.append(
                f"rejection filter '{reason}' blocked ~{oc:.2%}/trade of realized upside"
            )
    return lines


def _stage2_concerns(verdict: Optional[PromotionVerdict]) -> list[str]:
    if verdict is None:
        return []
    out: list[str] = []
    for c in getattr(verdict, "criteria", []) or []:
        if getattr(c, "passed", None) is False:
            name = getattr(c, "name", "criterion")
            detail = getattr(c, "detail", "")
            out.append(f"promotion criterion '{name}' failed — {detail}")
    return out


def _stage3_concerns(drift: Optional[DriftReport]) -> list[str]:
    if drift is None:
        return []
    out: list[str] = []
    for m in getattr(drift, "metrics", []) or []:
        if getattr(m, "material", False):
            out.append(
                f"drift ({getattr(m, 'kind', '?')}) — {getattr(m, 'name', '?')}: "
                f"{getattr(m, 'detail', '')}"
            )
    return out


def _stage4_concerns(
    spread_costs: Optional[SpreadCosts],
    expected_maker_benefit_pct: Optional[float],
) -> list[str]:
    out: list[str] = []
    if spread_costs is not None:
        overall = getattr(spread_costs, "overall_avg_spread_pct", None)
        if overall is not None and overall > 0.003:
            out.append(f"average entry spread cost {overall:.2%} exceeds 30 bps")
    if expected_maker_benefit_pct is not None and expected_maker_benefit_pct > 0.001:
        out.append(
            f"switching entries to maker limits looks +{expected_maker_benefit_pct:.2%}/trade "
            "in expectation"
        )
    return out


def unify_verdict(
    *,
    stage1_summary: Optional[list[dict]] = None,
    stage2_verdict: Optional[PromotionVerdict] = None,
    stage3_drift: Optional[DriftReport] = None,
    stage4_spread: Optional[SpreadCosts] = None,
    stage4_maker_benefit_pct: Optional[float] = None,
) -> VerdictCard:
    """Merge stage 1-4 outputs into a single advisory verdict card."""

    inputs_present = {
        "stage1_rejected_setups": stage1_summary is not None,
        "stage2_promotion": stage2_verdict is not None,
        "stage3_drift": stage3_drift is not None,
        "stage4_execution": stage4_spread is not None or stage4_maker_benefit_pct is not None,
    }

    reasons: list[str] = []
    recommendations: list[str] = []
    concerns: list[str] = []
    concerns += _stage1_concerns(stage1_summary)
    concerns += _stage2_concerns(stage2_verdict)
    concerns += _stage3_concerns(stage3_drift)
    concerns += _stage4_concerns(stage4_spread, stage4_maker_benefit_pct)

    exec_drift = _has_material_execution_drift(stage3_drift)
    dd_drift = _has_material_drawdown_drift(stage3_drift)
    s2 = (getattr(stage2_verdict, "verdict", "") or "").upper().replace(" ", "_")

    if exec_drift or dd_drift:
        overall = PAUSE
        if exec_drift:
            reasons.append("material execution drift — fills are costing more than modeled")
            recommendations.append(
                "Halve position size and switch entries to passive limits until the entry "
                "spread returns below the modeled slippage."
            )
        if dd_drift:
            reasons.append("live drawdown is materially worse than backtest")
            recommendations.append(
                "Cut position size to 50% and require Stage 2 to re-evaluate after "
                "20 more trades before restoring size."
            )
    elif s2 == "REJECT":
        overall = REJECT
        reasons.append("Stage 2 verdict is REJECT — adequate data shows no positive edge")
        recommendations.append(
            "Stop this live experiment. Return to paper trading with a parameter change "
            "or a new hypothesis before resuming."
        )
    elif s2 == "PROMOTE" and not exec_drift and not dd_drift and not (
        getattr(stage3_drift, "strategy_drift", False)
        or getattr(stage3_drift, "execution_drift", False)
    ):
        overall = PROMOTE
        reasons.append("Stage 2 verdict is PROMOTE with no material drift")
        recommendations.append(
            "Consider graduating this variant (wider universe or higher sizing). "
            "Review Stage 4 maker-vs-taker economics before increasing size."
        )
    else:
        overall = KEEP_TESTING
        if s2 == "PROMOTE":
            reasons.append("Stage 2 PROMOTE, but drift signals are still present")
        elif s2 in ("KEEP_TESTING", ""):
            reasons.append("insufficient evidence to promote and no confident rejection")
        recommendations.append(
            "Collect more forward evidence. Do not change parameters mid-experiment."
        )

    return VerdictCard(
        overall=overall,
        reasons=reasons,
        concerns=concerns,
        recommendations=recommendations,
        inputs_present=inputs_present,
    )
