"""Stage 2 — Live-trade statistics and promotion criteria (READ-ONLY).

Computes honest performance statistics from live ``closed_trades`` — trades,
win rate, avg win/loss, expectancy (with a bootstrap confidence interval),
profit factor, max drawdown, max losing streak, and MFE captured — split by BTC
regime and by exit reason. It then evaluates the pre-committed promotion
criteria (see ``docs/research/promotion_criteria.md``) and returns a
PROMOTE / KEEP TESTING / REJECT verdict with reasons.

Safety
------
Pure read-only analysis. Reads ``closed_trades`` via the sanctioned storage read
path and NEVER writes any table, changes any config, or touches an order path.
The verdict is advisory only — a human applies any change.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.research.bootstrap import bootstrap_ci
from app.storage import storage as live_storage

# Promotion thresholds — mirror docs/research/promotion_criteria.md exactly.
MIN_TRADES = 30
MIN_PROFIT_FACTOR = 1.5
MAX_CONCENTRATION = 0.40


def regime_label(score: Optional[int]) -> str:
    """Map a stored ``entry_btc_regime`` score (-2..+2) to a regime label,
    matching ``app.regime.btc_regime._label``. Missing score -> "unknown"."""
    if score is None:
        return "unknown"
    try:
        s = int(score)
    except (TypeError, ValueError):
        return "unknown"
    if s >= 2:
        return "STRONG_BULL"
    if s == 1:
        return "BULL"
    if s == 0:
        return "SIDEWAYS"
    if s == -1:
        return "BEAR"
    return "STRONG_BEAR"


def _num(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def trade_pnl(row: dict) -> float:
    """Realized PnL in USDT, preferring the fee-corrected value when present."""
    v = _num(row.get("pnl_corrected"))
    if v is None:
        v = _num(row.get("pnl"))
    return v or 0.0


def trade_pnl_pct(row: dict) -> float:
    """Realized per-trade return (fraction), preferring the corrected value."""
    v = _num(row.get("pnl_pct_corrected"))
    if v is None:
        v = _num(row.get("pnl_pct"))
    return (v or 0.0) / 100.0 if v is not None else 0.0


def _parse_ts(ts: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _sorted_by_exit(trades: list[dict]) -> list[dict]:
    return sorted(trades, key=lambda t: _parse_ts(t.get("exit_ts")) or datetime.min.replace(tzinfo=timezone.utc))


@dataclass
class Stats:
    n: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    avg_win_usd: float = 0.0
    avg_loss_usd: float = 0.0
    expectancy_usd: float = 0.0
    expectancy_pct: float = 0.0
    expectancy_pct_ci: tuple[float, float] = (float("nan"), float("nan"))
    profit_factor: float = 0.0
    gross_profit_usd: float = 0.0
    gross_loss_usd: float = 0.0
    net_pnl_usd: float = 0.0
    max_drawdown_usd: float = 0.0
    max_losing_streak: int = 0
    mfe_captured: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        d = self.__dict__.copy()
        d["expectancy_pct_ci"] = list(self.expectancy_pct_ci)
        return d


def compute_stats(trades: list[dict], *, ci_resamples: int = 2000) -> Stats:
    """Compute the full statistics block for a list of closed-trade rows."""
    if not trades:
        return Stats()
    ordered = _sorted_by_exit(trades)
    pnls = [trade_pnl(t) for t in ordered]
    pnl_pcts = [trade_pnl_pct(t) for t in ordered]

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    n = len(pnls)
    gross_profit = sum(wins)
    gross_loss = -sum(losses)
    profit_factor = (
        (gross_profit / gross_loss) if gross_loss > 0
        else float("inf") if gross_profit > 0 else 0.0
    )

    # Cumulative realized-PnL drawdown (USDT) and losing streak.
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    streak = 0
    max_streak = 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        if p <= 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0

    # MFE captured: for winners (not stopped out), how much of the peak
    # favorable move was realized.
    ratios: list[float] = []
    for t in ordered:
        mfe = _num(t.get("mfe_pct"))
        pct = trade_pnl_pct(t)
        reason = str(t.get("exit_reason") or "")
        if mfe and mfe > 0 and pct > 0 and "stop_loss" not in reason:
            ratios.append(min(pct / (mfe / 100.0), 1.0))
    mfe_captured = (sum(ratios) / len(ratios)) if ratios else 0.0

    ci = bootstrap_ci(pnl_pcts, n_resamples=ci_resamples) if n >= 2 else (
        (pnl_pcts[0], pnl_pcts[0]) if n == 1 else (float("nan"), float("nan"))
    )

    return Stats(
        n=n,
        wins=len(wins),
        losses=len(losses),
        win_rate=len(wins) / n,
        avg_win_usd=(sum(wins) / len(wins)) if wins else 0.0,
        avg_loss_usd=(sum(losses) / len(losses)) if losses else 0.0,
        expectancy_usd=sum(pnls) / n,
        expectancy_pct=sum(pnl_pcts) / n,
        expectancy_pct_ci=ci,
        profit_factor=profit_factor,
        gross_profit_usd=gross_profit,
        gross_loss_usd=gross_loss,
        net_pnl_usd=sum(pnls),
        max_drawdown_usd=max_dd,
        max_losing_streak=max_streak,
        mfe_captured=mfe_captured,
    )


def split_stats(trades: list[dict], key: Callable[[dict], str]) -> dict[str, Stats]:
    groups: dict[str, list[dict]] = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t)
    return {k: compute_stats(v) for k, v in groups.items()}


def observed_regimes(trades: list[dict]) -> set[str]:
    """Distinct non-"unknown" BTC regime labels the trades occurred under."""
    labels = {regime_label(_int_or_none(t.get("entry_btc_regime"))) for t in trades}
    labels.discard("unknown")
    return labels


def _int_or_none(x: Any) -> Optional[int]:
    if x is None:
        return None
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


@dataclass
class Concentration:
    max_trade_frac: float = 0.0
    max_symbol_frac: float = 0.0
    top_symbol: str = ""
    gross_profit_usd: float = 0.0


def concentration(trades: list[dict]) -> Concentration:
    """Largest single-trade and single-symbol share of total gross profit."""
    gross_profit = sum(p for p in (trade_pnl(t) for t in trades) if p > 0)
    if gross_profit <= 0:
        return Concentration(gross_profit_usd=gross_profit)

    max_trade = max((trade_pnl(t) for t in trades), default=0.0)
    by_symbol: dict[str, float] = {}
    for t in trades:
        p = trade_pnl(t)
        if p > 0:
            by_symbol[str(t.get("symbol") or "")] = by_symbol.get(str(t.get("symbol") or ""), 0.0) + p
    top_symbol, top_symbol_profit = max(by_symbol.items(), key=lambda kv: kv[1], default=("", 0.0))
    return Concentration(
        max_trade_frac=max(max_trade, 0.0) / gross_profit,
        max_symbol_frac=top_symbol_profit / gross_profit,
        top_symbol=top_symbol,
        gross_profit_usd=gross_profit,
    )


# ── Promotion evaluation ────────────────────────────────────────────────────

@dataclass
class Criterion:
    name: str
    passed: Optional[bool]  # True / False / None (cannot determine)
    detail: str


@dataclass
class PromotionVerdict:
    verdict: str  # "PROMOTE" | "KEEP TESTING" | "REJECT"
    criteria: list[Criterion] = field(default_factory=list)
    stats: Stats = field(default_factory=Stats)
    concentration: Concentration = field(default_factory=Concentration)
    regimes: set[str] = field(default_factory=set)


def evaluate_promotion(trades: list[dict]) -> PromotionVerdict:
    """Evaluate the pre-committed promotion criteria. Advisory only."""
    stats = compute_stats(trades)
    conc = concentration(trades)
    regimes = observed_regimes(trades)
    ci_lo, ci_hi = stats.expectancy_pct_ci

    criteria = [
        Criterion(
            "sample_size", stats.n >= MIN_TRADES,
            f"{stats.n} closed trades (need >= {MIN_TRADES})",
        ),
        Criterion(
            "regime_change",
            (len(regimes) >= 2) if regimes else None,
            f"observed regimes: {sorted(regimes) or 'unknown (no entry_btc_regime recorded)'} "
            f"(need >= 2 distinct)",
        ),
        Criterion(
            "positive_edge",
            (ci_lo > 0) if stats.n >= 2 else None,
            f"expectancy {stats.expectancy_pct:+.2%}/trade, 95% CI "
            f"[{ci_lo:+.2%}, {ci_hi:+.2%}] (need lower bound > 0)",
        ),
        Criterion(
            "profit_factor",
            stats.profit_factor > MIN_PROFIT_FACTOR,
            f"profit factor {_fmt_pf(stats.profit_factor)} (need > {MIN_PROFIT_FACTOR})",
        ),
        Criterion(
            "no_concentration",
            (conc.max_trade_frac <= MAX_CONCENTRATION and conc.max_symbol_frac <= MAX_CONCENTRATION)
            if conc.gross_profit_usd > 0 else None,
            f"max single trade {conc.max_trade_frac:.0%}, max symbol "
            f"{conc.max_symbol_frac:.0%} ({conc.top_symbol}) of gross profit "
            f"(need both <= {MAX_CONCENTRATION:.0%})",
        ),
    ]

    all_pass = all(c.passed is True for c in criteria)
    adequate = stats.n >= MIN_TRADES
    no_edge = adequate and ((stats.n >= 2 and ci_hi <= 0) or stats.profit_factor < 1.0)

    if all_pass:
        verdict = "PROMOTE"
    elif no_edge:
        verdict = "REJECT"
    else:
        verdict = "KEEP TESTING"

    return PromotionVerdict(
        verdict=verdict, criteria=criteria, stats=stats,
        concentration=conc, regimes=regimes,
    )


def _fmt_pf(pf: float) -> str:
    return "inf" if pf == float("inf") else f"{pf:.2f}"


def load_live_trades(*, mode: str = "live", limit: int = 100_000, source_storage=live_storage) -> list[dict]:
    """Read closed trades for ``mode`` (read-only)."""
    return [t for t in source_storage.closed_trades(limit=limit) if str(t.get("mode") or "") == mode]


def build_report(*, mode: str = "live", source_storage=live_storage) -> dict[str, Any]:
    """Full read-only report: overall stats, splits, and promotion verdict."""
    trades = load_live_trades(mode=mode, source_storage=source_storage)
    verdict = evaluate_promotion(trades)
    return {
        "mode": mode,
        "overall": verdict.stats,
        "by_regime": split_stats(trades, lambda t: regime_label(_int_or_none(t.get("entry_btc_regime")))),
        "by_exit_reason": split_stats(trades, lambda t: str(t.get("exit_reason") or "") or "unknown"),
        "verdict": verdict,
    }
