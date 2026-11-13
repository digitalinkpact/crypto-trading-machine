"""HTTP routes — dashboard, settings, autopilot controls, trades log."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import logging
from decimal import Decimal

from fastapi import APIRouter, Form, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import SYMBOLS, TIMEFRAMES, get_settings
from app.auth.service import current_user
from app.credentials import (
    credentials_present,
    save_binance_credentials,
    save_risk_settings,
    save_trading_mode,
)
from app.storage import storage
from app.trading import audit_labels
from app.trading.autopilot import autopilot
from app.trading.portfolio import portfolio_snapshot
from app.exchange.ws_stream import live_prices
from app.exchange.telemetry import exchange_telemetry
from app.trading.win_rate import win_rate_summary_from_storage

router = APIRouter()
_APP_STARTED_AT = datetime.now(timezone.utc)


_CSS = """
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #0b1020; color: #e6e8ef; min-height: 100vh; }
header { padding: 1rem 1.5rem; border-bottom: 1px solid #1d2540;
         display: flex; justify-content: space-between; align-items: center; }
header h1 { margin: 0; font-size: 1.1rem; }
header a { color: #7aa6ff; text-decoration: none; font-size: 0.9rem; }
header a:hover { text-decoration: underline; }
header a.active { color: #e6e8ef; font-weight: 600; }
main { max-width: 800px; margin: 0 auto; padding: 2rem 1.5rem; }
.card { background: #121831; border: 1px solid #1d2540; border-radius: 12px;
        padding: 1.5rem; margin-bottom: 1rem; }
.card h2 { margin: 0 0 0.5rem; font-size: 1rem; color: #c8cee8; }
.muted { color: #8a93b2; font-size: 0.9rem; }
.row { display: flex; justify-content: space-between; padding: 0.4rem 0;
       border-bottom: 1px solid #1d2540; font-size: 0.9rem; }
.row:last-child { border-bottom: 0; }
.pill { display: inline-block; font-size: 0.8rem; padding: 0.2rem 0.7rem;
        border-radius: 999px; background: #1d2540; color: #8a93b2;
        margin-left: 0.5rem; }
.pill.live { background: #0e3b27; color: #5be29a; }
.pill.off { background: #2a1d1d; color: #ff7a8a; }
.pill.paper { background: #1a2a4a; color: #7aa6ff; }
.pill.real { background: #4a2418; color: #ffb685; }
.btn { display: block; width: 100%; padding: 1rem; font-size: 1.05rem;
       font-weight: 600; border: 0; border-radius: 10px; cursor: pointer;
       margin-top: 0.5rem; color: white; }
.btn-start { background: #0e7a3f; }
.btn-start:hover { background: #109a4e; }
.btn-stop { background: #b3263a; }
.btn-stop:hover { background: #d52d44; }
.btn-mode { background: #2a3a6a; }
.btn-mode:hover { background: #3a4f8f; }
.btn:disabled { opacity: 0.45; cursor: not-allowed; }
input[type=text], input[type=password] {
  width: 100%; padding: 0.6rem 0.75rem; background: #0b1020;
  border: 1px solid #1d2540; border-radius: 6px; color: #e6e8ef;
  font-size: 0.9rem; margin-top: 0.25rem; }
label { display: block; font-size: 0.85rem; color: #8a93b2; margin-top: 0.75rem; }
.banner { padding: 0.75rem 1rem; border-radius: 8px; margin-bottom: 1rem;
          font-size: 0.9rem; }
.banner.warn { background: #3b2a0e; color: #f0c45b; border: 1px solid #5a4214; }
.banner.ok { background: #0e3b27; color: #5be29a; border: 1px solid #14593a; }
.banner.danger { background: #3b0e1a; color: #ff7a8a; border: 1px solid #5a1424; }
form { margin: 0; }
table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
th, td { text-align: left; padding: 0.4rem 0.5rem; border-bottom: 1px solid #1d2540; }
th { color: #8a93b2; font-weight: 600; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.win { color: #5be29a; }
.loss { color: #ff7a8a; }
header, nav { flex-wrap: wrap; gap: 0.75rem; }
.row { gap: 1rem; flex-wrap: wrap; }
.row b { min-width: 0; overflow-wrap: anywhere; }
pre { max-width: 100%; white-space: pre-wrap; overflow-wrap: anywhere; }
@media (max-width: 600px) {
    main { padding: 1rem; }
    .card { padding: 1rem; overflow-x: auto; }
    header { padding: 1rem; }
}
"""


def _layout(title: str, body: str, active: str = "home") -> str:
    nav = []
    for slug, label in (
        ("home", "Dashboard"),
        ("trades", "Trades"),
        ("settings", "Settings"),
        ("audit", "Audit"),
        ("account", "Account"),
        ("logout", "Sign out"),
    ):
        cls = "active" if active == slug else ""
        if slug == "home":
            href = "/"
        elif slug == "audit":
            href = "/auth/audit"
        elif slug == "account":
            href = "/auth/password"
        elif slug == "logout":
            href = "/auth/logout"
        else:
            href = f"/{slug}"
        nav.append(f"<a class='{cls}' href='{href}'>{label}</a>")
    return (
        "<!doctype html><html><head><meta charset='utf-8'/>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'/>"
        f"<title>{title}</title><style>{_CSS}</style></head><body>"
        "<header><h1>AI Crypto Trading Machine</h1><nav>"
        + " &nbsp;&middot;&nbsp; ".join(nav)
        + f"</nav></header><main>{body}</main></body></html>"
    )


def _mode_pill() -> str:
    s = get_settings()
    if s.paper_trading:
        return "<span class='pill paper'>SIMULATION</span>"
    return "<span class='pill real'>LIVE &mdash; REAL MONEY</span>"


def _audit_stage_text(row: dict) -> str:
    """Dashboard text for a trade_audit row; internal gates never read as Binance."""
    if not row:
        return "Unavailable"
    stage = audit_labels.execution_stage(row)
    text = audit_labels.STAGE_LABELS[stage]
    if stage == audit_labels.INTERNAL_REJECTED:
        text += f" ({audit_labels.rejection_reason(row)})"
    elif stage == audit_labels.SUBMITTED_NOT_FILLED:
        text += f" (exchange status {row.get('binance_response')})"
    return text


def _execution_overview(snapshot: dict | None) -> dict:
    from app.trading.watchdog import get_scheduler_status

    mode = get_settings().ledger_mode
    runs = storage.recent_tick_runs(1, mode=mode)
    latest = runs[0] if runs else None
    positions = [position for position in storage.all_positions() if position["mode"] == mode]
    prices = {holding["asset"]: Decimal(str(holding["price_usdt"])) for holding in (snapshot or {}).get("holdings", [])}
    unrealized = Decimal("0")
    for position in positions:
        price = prices.get(position["symbol"].removesuffix("USDT"))
        if price is None:
            unrealized = None
            break
        unrealized += Decimal(str(position["qty"])) * (price - Decimal(str(position["entry_price"])))
    return {
        "trading_mode": get_settings().trading_mode.value,
        "ledger_mode": mode,
        "mode_consistent": autopilot.state.mode == mode,
        "latest_tick": latest,
        "scheduler": get_scheduler_status(),
        "last_successful_tick_at": storage.kv_get("autopilot_last_success"),
        "last_completed_tick_at": storage.kv_get("autopilot_last_completed_tick"),
        "last_technical_error": storage.kv_get("autopilot_last_technical_error"),
        "risk_evaluation": storage.kv_get("risk_loop_status"),
        "app_started_at": _APP_STARTED_AT.isoformat(),
        "open_positions": len(positions),
        "orders": storage.order_summary(mode),
        "fill_reconciliation": storage.kv_get("fill_reconciliation_last_run"),
        "available_usdt": str(snapshot["usdt_cash"]) if snapshot is not None else None,
        "realized_pnl_usdt": storage.total_realized_pnl(mode=mode),
        "unrealized_pnl_usdt": str(unrealized) if unrealized is not None else None,
    }


def _render_execution_panel(overview: dict) -> str:
    latest = overview["latest_tick"] or {}
    decisions = latest.get("decisions", {})
    values = {
        "Mode": overview["trading_mode"].upper(),
        "Mode agreement": "OK" if overview["mode_consistent"] else "ERROR",
        "Tick ID": latest.get("tick_id"),
        "Last tick result": latest.get("status"),
        "Tick started": latest.get("started_at"),
        "Tick completed": latest.get("completed_at"),
        "Tick duration (seconds)": latest.get("duration_seconds"),
        "Next scheduled tick": overview["scheduler"].get("next_tick_at"),
        "Last successful tick": overview["last_successful_tick_at"],
        "Last technical error": overview["last_technical_error"],
        "Pairs scanned": latest.get("pairs_evaluated"),
        "BUY decisions": decisions.get("BUY"),
        "SELL decisions": decisions.get("SELL"),
        "HOLD decisions": decisions.get("HOLD"),
        "Risk rejections": latest.get("risk_rejections"),
        "Orders submitted (tick)": latest.get("orders_submitted"),
        "Orders filled (tick)": latest.get("orders_filled"),
        "Execution errors": latest.get("errors"),
        "Open positions": overview["open_positions"],
        "Pending or unresolved orders": overview["orders"]["pending"],
        "Recorded filled orders": overview["orders"]["filled"],
        "Historical orders with unknown status": overview["orders"]["legacy_unknown"],
        "Available USDT": overview["available_usdt"],
        "Realized P&L (USDT)": overview["realized_pnl_usdt"],
        "Unrealized P&L before exit fees (USDT)": overview["unrealized_pnl_usdt"],
        "Risk evaluation": overview["risk_evaluation"],
        "App started": overview["app_started_at"],
    }
    rows = "".join(
        f"<div class='row'><span>{escape(label)}</span><b>{escape(str(value) if value is not None else 'Unavailable')}</b></div>"
        for label, value in values.items()
    )
    return f"<section class='card'><h2>Execution status</h2>{rows}</section>"


_MANUAL_TICK_SCRIPT = """
<script>
document.getElementById('run-one-tick').addEventListener('click', async event => {
  const button = event.currentTarget;
  const output = document.getElementById('manual-tick-result');
  const confirmation = button.dataset.confirmation;
  if (confirmation === 'RUN_ONE_LIVE_TICK' &&
      window.prompt('Type RUN_ONE_LIVE_TICK to confirm real-money execution') !== confirmation) return;
  button.disabled = true;
  output.textContent = 'RUNNING';
  try {
    const response = await fetch('/autopilot/tick', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Accept': 'application/json', 'X-Tick-Confirmation': confirmation}
    });
    if (response.status === 401) throw new Error('Authentication required');
    const result = await response.json();
    output.textContent = JSON.stringify(result, null, 2);
  } catch (error) {
    output.textContent = 'Result unknown: ' + error.message + '. Inspect persisted ticks and orders before retrying.';
  } finally { button.disabled = false; }
});
</script>
"""


def _render_tick_diagnostics() -> tuple[str, str]:
    data = storage.kv_get("autopilot_last_tick_debug") or {}
    reasons = data.get("by_reason") or {}
    per_symbol = data.get("per_symbol") or {}
    scan = storage.kv_get("profitstream_scan") or {}
    scan_html = ""
    if scan:
        breadth = scan.get("breadth") or {}
        scan_html = (
            "<div class='card'><h2>Daily strategy scan</h2>"
            f"<div>Closed candle: {escape(str(scan.get('candle_close_at') or 'unavailable'))}</div>"
            f"<div>{escape(str(scan.get('error') or ''))}</div>"
            f"<div>Analyzed: {int(scan.get('symbols', 0))}; "
            f"ready: {int(scan.get('usable_symbols', 0))}; "
            f"highest score: {int(scan.get('highest_score', 0))}/"
            f"{int(scan.get('score_threshold', 80))} required</div>"
            f"<div>BTC regime: {escape(str(scan.get('btc_regime_label', 'unknown')))}; "
            f"market policy allows entries: {'yes' if scan.get('market_allowed') else 'no'}; "
            f"strong bull required: {'yes' if scan.get('strong_bull_required') else 'no'}</div>"
            f"<div>Alt breadth: {int(breadth.get('above', 0))}/"
            f"{int(breadth.get('usable', 0))} above EMA{int(breadth.get('ema_span', 20))}; "
            f"coverage {int(breadth.get('usable', 0))}/{int(breadth.get('expected', 0))}; "
            f"allows entries: {'yes' if breadth.get('allowed') else 'no'}</div>"
            "<div class='muted'>Repeated scans of the same closed daily candle are not "
            "independent opportunities. Entry eligibility does not mean an order was submitted.</div>"
            + "".join(
                f"<div class='row'><span>{escape(str(name))}</span><b>{int(count)} pass</b></div>"
                for name, count in sorted((scan.get("predicate_pass_counts") or {}).items())
            )
            + "</div>"
        )
    market_hits = int(reasons.get("market_gate", 0) or 0)
    banner = ""
    if market_hits > 0:
        banner = (
            "<div class='banner warn'>"
            f"Last tick blocked {market_hits} BUY signal(s) at the BTC market-regime gate. "
            "Review the diagnostics card below to confirm the regime and downstream sizing checks."
            "</div>"
        )

    if not data:
        return banner, scan_html + (
            "<div class='card'><h2>Last tick diagnostics</h2>"
            "<div class='muted'>No tick diagnostics recorded yet.</div></div>"
        )

    reason_rows = "".join(
        f"<div class='row'><span>{escape(str(reason))}</span><b>{int(count)}</b></div>"
        for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[:8]
    ) or "<div class='row muted'><span>No skip reasons</span><span>&mdash;</span></div>"

    buy_entries = [
        (symbol, info) for symbol, info in per_symbol.items()
        if isinstance(info, dict) and info.get("action") == "BUY"
    ]
    buy_rows = ""
    for symbol, info in buy_entries[:8]:
        filters_info = info.get("filters") or {}
        market = filters_info.get("market_regime") or {}
        min_check = filters_info.get("min_notional") or {}
        sizing = info.get("sizing") or {}
        qty = escape(str(sizing.get("rounded_qty", "-")))
        notional = escape(str(sizing.get("notional", "-")))
        final_reason = escape(str(info.get("final_reason") or info.get("reason") or "-"))
        detail = escape(str(info.get("detail") or ""))
        market_detail = escape(str(market.get("detail") or "not evaluated"))
        min_detail = escape(str(min_check.get("detail") or "not evaluated"))
        submitted = "yes" if info.get("submitted") else "no"
        buy_rows += (
            "<tr>"
            f"<td>{escape(symbol)}</td>"
            f"<td class='num'>{float(info.get('confidence', 0.0)):.2f}</td>"
            f"<td>{market_detail}</td>"
            f"<td class='num'>{qty}</td>"
            f"<td class='num'>{notional}</td>"
            f"<td>{min_detail}</td>"
            f"<td>{submitted}</td>"
            f"<td>{final_reason}<br/><span class='muted'>{detail}</span></td>"
            "</tr>"
        )
    if buy_rows:
        buy_table = (
            "<table style='margin-top:0.75rem;'><thead><tr>"
            "<th>BUY symbol</th><th class='num'>Conf</th><th>Market regime</th>"
            "<th class='num'>Qty</th><th class='num'>Notional</th><th>Min check</th>"
            "<th>Submitted</th><th>Final</th></tr></thead>"
            f"<tbody>{buy_rows}</tbody></table>"
        )
    else:
        buy_table = "<div class='muted' style='margin-top:0.75rem;'>No BUY traces recorded on the last tick.</div>"

    card = f"""
<div class='card'>
  <h2>Last tick diagnostics</h2>
  <div class='muted'>Signals: {int(data.get('total_signals', 0) or 0)} &middot; Timestamp: {escape(str(data.get('ts') or '&mdash;'))}</div>
  {reason_rows}
  {buy_table}
</div>
"""
    return banner, scan_html + card


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> str:
    s = get_settings()
    is_paper = s.paper_trading
    creds = credentials_present()
    st = autopilot.state
    pill_class = "live" if st.running else "off"
    health_state = storage.kv_get("health_status") or {}
    pill_text = "ERROR" if st.running and health_state.get("overall_healthy") is False else "RUNNING" if st.running else "STOPPED"
    diag_banner, diag_card = _render_tick_diagnostics()

    if is_paper:
        creds_banner = (
            "<div class='banner ok'>SIMULATION &mdash; isolated ledger; no real orders.</div>"
        )
    elif not creds:
        creds_banner = (
            "<div class='banner warn'>LIVE mode is selected but no Binance.US API "
            "keys are saved. <a href='/settings' style='color:#f0c45b;'>Add them on "
            "Settings</a> before starting.</div>"
        )
    else:
        creds_banner = (
            "<div class='banner danger'>LIVE mode &mdash; orders use REAL MONEY on "
            "Binance.US. Switch to Paper on Settings if unsure.</div>"
        )

    balance_card = ""
    snap = None
    low_cash_banner = ""
    try:
        snap = await portfolio_snapshot()
        total = snap["total_usdt"]
        cash = snap["usdt_cash"]
        invested = total - cash
        if not is_paper and cash < 10:
            low_cash_banner = (
                "<div class='banner warn'>LIVE mode has less than 10 USDT free. "
                "New BUYs will fail the minimum trade sizing check until more USDT is available.</div>"
            )
        baseline = st.starting_balance_usdt
        if baseline and baseline > 0:
            pnl = total - baseline
            pnl_pct = (pnl / baseline) * 100
            pnl_color = "#5be29a" if pnl >= 0 else "#ff7a8a"
            pnl_sign = "+" if pnl >= 0 else ""
            pnl_row = (
                f"<div class='row'><span>P&amp;L since start</span>"
                f"<b style='color:{pnl_color};'>{pnl_sign}${pnl:,.2f} "
                f"({pnl_sign}{pnl_pct:.2f}%)</b></div>"
                f"<div class='row'><span>Starting balance</span>"
                f"<b>${baseline:,.2f}</b></div>"
            )
        else:
            pnl_row = (
                "<div class='row muted'><span>P&amp;L</span>"
                "<span>Start autopilot to begin tracking</span></div>"
            )
        top_holdings = sorted(
            snap["holdings"], key=lambda h: h["value_usdt"], reverse=True
        )[:5]
        holdings_html = "".join(
            f"<div class='row'><span>{h['asset']}</span>"
            f"<b>${h['value_usdt']:,.2f}</b></div>"
            for h in top_holdings
        ) or "<div class='row muted'><span>No holdings</span><span>&mdash;</span></div>"
        balance_card = f"""
<div class='card'>
  <h2>Portfolio {_mode_pill()}</h2>
  <div class='row'><span>Total balance</span><b>${total:,.2f}</b></div>
  <div class='row'><span>USDT cash</span><b>${cash:,.2f}</b></div>
  <div class='row'><span>Invested</span><b>${invested:,.2f}</b></div>
  {pnl_row}
  <div class='muted' style='margin-top:0.75rem;'>Top holdings</div>
  {holdings_html}
</div>
        """
    except Exception as exc:  # noqa: BLE001
        balance_card = (
            f"<div class='card'><h2>Portfolio {_mode_pill()}</h2>"
            f"<div class='muted'>Could not fetch balance: {exc}</div></div>"
        )

    stats = storage.agent_stats(mode=s.ledger_mode)
    if stats:
        rows = "".join(
            f"<tr><td>{a['agent']}</td>"
            f"<td class='num'>{a['total_trades']}</td>"
            f"<td class='num'>{a['win_rate']*100:.1f}%</td>"
            f"<td class='num {'win' if a['total_pnl']>=0 else 'loss'}'>"
            f"${a['total_pnl']:,.2f}</td></tr>"
            for a in stats
        )
        agent_card = f"""
<div class='card'>
  <h2>Agent learning</h2>
  <table style='margin-top:0.75rem;'>
    <thead><tr><th>Agent</th><th class='num'>Positions</th><th class='num'>Win rate (per position)</th><th class='num'>Total P&amp;L</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
        """
    else:
        agent_card = (
            "<div class='card'><h2>Agent learning</h2>"
            "<div class='muted'>No closed positions in this mode.</div></div>"
        )

    # ── Risk events card ─────────────────────────────────────────────
    risk_events = storage.recent_risk_events(limit=10, mode=s.ledger_mode)
    if risk_events:
        rows = "".join(
            f"<tr><td>{e['exit_ts'][:16].replace('T',' ')}</td>"
            f"<td>{e['symbol']}</td>"
            f"<td><span class='pill'>{e['reason']}</span></td>"
            f"<td class='num {'win' if e['pnl']>=0 else 'loss'}'>"
            f"${float(e['pnl']):,.2f} ({float(e['pnl_pct']):+.2f}%)</td></tr>"
            for e in risk_events
        )
        risk_card = f"""
<div class='card'>
  <h2>Risk gate exits</h2>
  <div class='muted'>Forced exits from stop-loss, take-profit, trailing stop, or max-hold. These prove the safety rails are firing.</div>
  <table style='margin-top:0.75rem;'>
    <thead><tr><th>Time</th><th>Symbol</th><th>Reason</th><th class='num'>P&amp;L</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
        """
    else:
        risk_card = (
            "<div class='card'><h2>Risk gate exits</h2>"
            "<div class='muted'>No risk-gate exits yet. They appear here when "
            "stop-loss, take-profit, trailing-stop, or max-hold force a position out.</div></div>"
        )

    # ── Equity curve card (last 30 snapshots, ascii sparkline) ───────
    curve = storage.equity_curve(limit=30, mode=s.ledger_mode)
    if len(curve) >= 2:
        values = [c["total_usdt"] for c in curve]
        lo, hi = min(values), max(values)
        spark_chars = "▁▂▃▄▅▆▇█"
        if hi > lo:
            sparks = "".join(
                spark_chars[min(7, int((v - lo) / (hi - lo) * 7))]
                for v in values
            )
        else:
            sparks = spark_chars[3] * len(values)
        first, last = values[0], values[-1]
        delta = last - first
        delta_pct = (delta / first * 100) if first else 0
        delta_color = "#5be29a" if delta >= 0 else "#ff7a8a"
        delta_sign = "+" if delta >= 0 else ""
        equity_card = f"""
<div class='card'>
  <h2>Equity curve <span class='muted' style='font-weight:400;'>(last {len(values)} snapshots)</span></h2>
  <div style='font-size:1.5rem; letter-spacing:1px; color:#7aa6ff; padding:0.5rem 0;'>{sparks}</div>
  <div class='row'><span>Range</span><b>${lo:,.2f} &mdash; ${hi:,.2f}</b></div>
  <div class='row'><span>Change over period</span>
    <b style='color:{delta_color};'>{delta_sign}${delta:,.2f} ({delta_sign}{delta_pct:.2f}%)</b>
  </div>
</div>
        """
    else:
        equity_card = (
            "<div class='card'><h2>Equity curve</h2>"
            "<div class='muted'>Snapshots are recorded hourly. Curve will appear once a few are collected.</div></div>"
        )

    last_tick = st.last_tick_at.strftime("%Y-%m-%d %H:%M UTC") if st.last_tick_at else "&mdash;"
    started = st.started_at.strftime("%Y-%m-%d %H:%M UTC") if st.started_at else "&mdash;"
    last_err = (
        f"<div class='row'><span>Last error</span>"
        f"<b style='color:#ff7a8a;'>{st.last_error}</b></div>"
        if st.last_error else ""
    )

    can_start = (creds or is_paper) and not st.running
    start_disabled = "" if can_start else "disabled"
    stop_disabled = "" if st.running else "disabled"
    overview = _execution_overview(snap)
    execution_panel = _render_execution_panel(overview)
    confirmation = "RUN_ONE_PAPER_TICK" if is_paper else "RUN_ONE_LIVE_TICK"
    recent_audit = storage.recent_trade_audit(limit=1, mode=s.ledger_mode)
    recent_orders = storage.recent_orders(limit=1, mode=s.ledger_mode)
    latest_audit = recent_audit[0] if recent_audit else {}
    latest_order = recent_orders[0] if recent_orders else {}

    body = f"""
{creds_banner}
{diag_banner}
{low_cash_banner}
<div class='card'>
  <h2>Autopilot <span class='pill {pill_class}'>{pill_text}</span> {_mode_pill()}</h2>
  <div class='row'><span>Started</span><b>{started}</b></div>
  <div class='row'><span>Last tick</span><b>{last_tick}</b></div>
  <div class='row'><span>Trades executed (this run)</span><b>{st.trades_executed}</b></div>
  {last_err}
    <button id='run-one-tick' class='btn btn-mode' data-confirmation='{confirmation}' {stop_disabled}>&#8635; RUN ONE TICK</button>
    <pre id='manual-tick-result' role='status' aria-live='polite'></pre>
  <form method='post' action='/autopilot/start'>
    <button class='btn btn-start' {start_disabled}>&#9654;  Start auto-trading</button>
  </form>
  <form method='post' action='/autopilot/stop'
        onsubmit="return confirm('Stop autopilot and SELL all holdings to USDT?');">
    <button class='btn btn-stop' {stop_disabled}>&#9632;  Stop &amp; liquidate everything</button>
  </form>
</div>
{execution_panel}
{balance_card}
{diag_card}
{equity_card}
{agent_card}
{risk_card}
<div class='card'>
    <h2>Trading monitor {_mode_pill()}</h2>
    <div class='row'><span>Last signal</span><b>{escape(str(latest_audit.get('signal') or 'Unavailable'))}</b></div>
    <div class='row'><span>Last order</span><b>{escape(str(latest_order.get('side', '') + ' ' + latest_order.get('symbol', '')).strip() or 'Unavailable')}</b></div>
    <div class='row'><span>Last execution stage</span><b>{escape(_audit_stage_text(latest_audit))}</b></div>
    <div class='row'><span>Last exception</span><b>{escape(str(latest_audit.get('exception') or st.last_error or 'Unavailable'))}</b></div>
    <div class='row'><span>Open positions</span><b>{len([p for p in storage.all_positions() if p['mode'] == st.mode])}</b></div>
    <div class='row'><span>Account assets</span><b>{len(snap.get('all_balances', {})) if snap is not None else 'Unavailable'}</b></div>
    <div class='row'><span>App uptime</span><b>{escape(str(datetime.now(timezone.utc) - _APP_STARTED_AT))}</b></div>
</div>
<div class='card'>
  <h2>Universe</h2>
  <div class='muted'>{len(SYMBOLS)} symbols &middot; {len(TIMEFRAMES)} timeframes
    ({", ".join(t.value for t in TIMEFRAMES)})</div>
</div>
"""
    return _layout("Dashboard", body + _MANUAL_TICK_SCRIPT, active="home")


@router.get("/trades", response_class=HTMLResponse, include_in_schema=False)
async def trades_page() -> str:
    closed = storage.closed_trades(limit=50)
    orders = storage.recent_orders(limit=50)

    if closed:
        rows = "".join(
            f"<tr><td>{t['exit_ts'][:16].replace('T',' ')}</td>"
            f"<td>{t['symbol']}</td>"
            f"<td><span class='pill {('paper' if t['mode']=='paper' else 'real')}'>{t['mode']}</span></td>"
            f"<td class='num'>{float(t['qty']):.4f}</td>"
            f"<td class='num'>${float(t['entry_price']):,.4f}</td>"
            f"<td class='num'>${float(t['exit_price']):,.4f}</td>"
            f"<td class='num {'win' if t['pnl']>=0 else 'loss'}'>"
            f"${float(t['pnl']):,.2f} ({float(t['pnl_pct']):+.2f}%)</td></tr>"
            for t in closed
        )
        closed_html = (
            "<table><thead><tr><th>Closed</th><th>Symbol</th><th>Mode</th>"
            "<th class='num'>Qty</th><th class='num'>Entry</th>"
            "<th class='num'>Exit</th><th class='num'>P&amp;L</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    else:
        closed_html = "<div class='muted'>No closed trades yet.</div>"

    if orders:
        rows = "".join(
            f"<tr><td>{o['ts'][:16].replace('T',' ')}</td>"
            f"<td>{o['symbol']}</td>"
            f"<td><span class='pill {('paper' if o['mode']=='paper' else 'real')}'>{o['mode']}</span></td>"
            f"<td class='{('win' if o['side']=='BUY' else 'loss')}'>{o['side']}</td>"
            f"<td class='num'>{float(o['qty']):.6f}</td>"
            f"<td class='num'>${float(o['price']):,.4f}</td></tr>"
            for o in orders
        )
        orders_html = (
            "<table><thead><tr><th>Time</th><th>Symbol</th><th>Mode</th>"
            "<th>Side</th><th class='num'>Qty</th>"
            "<th class='num'>Price</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
    else:
        orders_html = "<div class='muted'>No orders yet.</div>"

    body = f"""
<div class='card'>
  <h2>Closed trades</h2>
  {closed_html}
</div>
<div class='card'>
  <h2>Recent orders</h2>
  {orders_html}
</div>
"""
    return _layout("Trades", body, active="trades")


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(saved: int = 0, mode_saved: int = 0, risk_saved: int = 0) -> str:
    s = get_settings()
    creds = credentials_present()
    saved_banner = "<div class='banner ok'>API keys saved.</div>" if saved else ""
    if mode_saved:
        saved_banner += "<div class='banner ok'>Trading mode updated.</div>"
    if risk_saved:
        saved_banner += "<div class='banner ok'>Risk settings saved &mdash; restart not required.</div>"
    status = "API keys are set." if creds else "No API keys saved yet."

    is_paper = s.paper_trading
    paper_checked = "checked" if is_paper else ""
    live_checked = "" if is_paper else "checked"

    risk_form = f"""
<div class='card'>
  <h2>Risk gates</h2>
  <div class='muted'>Hard rules evaluated every tick. Tighten on losing streaks; loosen if you're being chopped out too quickly.</div>
  <form method='post' action='/settings/risk'>
    <label>Stop-loss % (decimal, e.g. 0.02 = 2%)
      <input type='text' name='stop_loss_pct' value='{s.stop_loss_pct}' />
    </label>
    <label>Take-profit %
      <input type='text' name='take_profit_pct' value='{s.take_profit_pct}' />
    </label>
    <label>Trailing-stop % (from peak after position is up half the take-profit)
      <input type='text' name='trailing_stop_pct' value='{s.trailing_stop_pct}' />
    </label>
    <label>Max hold (hours)
      <input type='text' name='max_hold_hours' value='{s.max_hold_hours}' />
    </label>
    <label>Drawdown circuit breaker % (halt new BUYs when total P&amp;L below this)
      <input type='text' name='drawdown_circuit_breaker_pct' value='{s.drawdown_circuit_breaker_pct}' />
    </label>
    <label>Min signal confidence (0&ndash;1)
      <input type='text' name='min_signal_confidence' value='{s.min_signal_confidence}' />
    </label>
    <label>Max position % per trade
      <input type='text' name='max_position_pct' value='{s.max_position_pct}' />
    </label>
    <label>Max concurrent open positions
      <input type='text' name='max_open_positions' value='{s.max_open_positions}' />
    </label>
    <button class='btn btn-mode' style='margin-top:1rem;'>Save risk settings</button>
  </form>
</div>
"""

    body = f"""
{saved_banner}
<div class='card'>
  <h2>Trading mode {_mode_pill()}</h2>
    <div class='row'><span>Execution</span><b>{escape(s.trading_mode.value.upper())}</b></div>
    <div class='row'><span>Mode changes</span><b>Offline migration required</b></div>
</div>
{risk_form}
<div class='card'>
  <h2>Binance.US API credentials</h2>
  <div class='muted'>{status} Keys are written to .env (gitignored)
    and used only to place orders on Binance.US.</div>
  <form method='post' action='/settings/credentials'>
    <label>API key
      <input type='text' name='api_key' autocomplete='off' required />
    </label>
    <label>API secret
      <input type='password' name='api_secret' autocomplete='off' required />
    </label>
    <button class='btn btn-start' style='margin-top:1rem;'>Save credentials</button>
  </form>
</div>
"""
    return _layout("Settings", body, active="settings")


@router.post("/settings/credentials", include_in_schema=False)
async def save_credentials(
    api_key: str = Form(...),
    api_secret: str = Form(...),
):
    try:
        save_binance_credentials(api_key, api_secret)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@router.post("/settings/mode", include_in_schema=False)
async def save_mode(mode: str = Form(...)):
    raise HTTPException(
        status_code=409,
        detail="Mode changes require an offline, reviewed TRADING_MODE migration and restart; live configuration was not changed",
    )


@router.post("/settings/risk", include_in_schema=False)
async def save_risk(
    stop_loss_pct: str = Form(""),
    take_profit_pct: str = Form(""),
    trailing_stop_pct: str = Form(""),
    max_hold_hours: str = Form(""),
    drawdown_circuit_breaker_pct: str = Form(""),
    min_signal_confidence: str = Form(""),
    max_position_pct: str = Form(""),
    max_open_positions: str = Form(""),
):
    try:
        save_risk_settings({
            "stop_loss_pct": stop_loss_pct,
            "take_profit_pct": take_profit_pct,
            "trailing_stop_pct": trailing_stop_pct,
            "max_hold_hours": max_hold_hours,
            "drawdown_circuit_breaker_pct": drawdown_circuit_breaker_pct,
            "min_signal_confidence": min_signal_confidence,
            "max_position_pct": max_position_pct,
            "max_open_positions": max_open_positions,
        })
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/settings?risk_saved=1", status_code=303)


@router.post("/autopilot/start", include_in_schema=False)
async def autopilot_start():
    try:
        await autopilot.start()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/", status_code=303)


@router.post("/autopilot/tick")
async def autopilot_tick(
    request: Request,
    confirmation: str = Header(default="", alias="X-Tick-Confirmation"),
) -> dict:
    user = current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="session authentication required")
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="same-origin request required")
    settings = get_settings()
    live = autopilot.state.mode != "paper" or not settings.paper_trading or settings.live_mode
    expected = "RUN_ONE_LIVE_TICK" if live else "RUN_ONE_PAPER_TICK"
    if confirmation != expected:
        raise HTTPException(status_code=400, detail=f"X-Tick-Confirmation must be {expected}")
    try:
        return await autopilot.tick(source="manual", requested_by=user.get("id"))
    except Exception as exc:
        logging.getLogger(__name__).exception("Manual autopilot tick failed")
        raise HTTPException(status_code=500, detail={
            "status": "FAILED",
            "reasons": ["tick_failed; inspect service logs before retrying"],
            "orders_submitted": None,
        }) from exc


@router.post("/autopilot/stop", include_in_schema=False)
async def autopilot_stop():
    await autopilot.stop_and_liquidate()
    return RedirectResponse(url="/", status_code=303)


@router.post("/autopilot/drawdown-recovery", include_in_schema=False)
async def autopilot_drawdown_recovery(
    confirmation: str = Form(...),
    reason: str = Form(...),
):
    if confirmation != "RESUME_LIVE_ENTRIES":
        raise HTTPException(status_code=400, detail="explicit confirmation is required")
    try:
        await autopilot.resume_after_drawdown_halt(reason=reason)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/", status_code=303)


@router.get("/health")
async def health(response: Response) -> dict:
    """Liveness+readiness probe for external supervisors (systemd timer,
    watchdog.sh, uptime monitors). Returns HTTP 200 only when the process is
    alive AND the last Binance.US price update is fresh (<`live_price_max_age_
    seconds`, default 30s) AND the local SQLite DB is writable. Any failing
    check drops the status to 503 so a monitor can tell "up but broken" apart
    from "actually fine".
    """
    s = get_settings()
    problems: list[str] = []

    ws_status = live_prices.status()
    price_age = ws_status.get("last_msg_age_s")
    price_fresh = True
    if ws_status.get("enabled"):
        if price_age is None or price_age > s.live_price_max_age_seconds:
            price_fresh = False
            problems.append(
                f"last Binance.US price update {price_age if price_age is not None else 'never'}s "
                f"ago (max {s.live_price_max_age_seconds}s)"
            )

    db_writable = True
    try:
        storage.kv_set("healthz_probe", {"ts": datetime.now(timezone.utc).isoformat()})
    except Exception as exc:  # noqa: BLE001
        db_writable = False
        problems.append(f"db not writable: {exc}")

    body = {
        "status": "ok" if not problems else "degraded",
        "process_alive": True,
        "price_fresh": price_fresh,
        "price_age_s": price_age,
        "db_writable": db_writable,
        "problems": problems,
    }
    if problems:
        response.status_code = 503
    return body


@router.get("/autopilot/status")
async def autopilot_status() -> dict:
    s = autopilot.state
    entry_status = storage.kv_get("entry_status") or {}
    try:
        snapshot = await portfolio_snapshot(mode=get_settings().ledger_mode)
    except Exception:
        snapshot = None
    return {
        **_execution_overview(snapshot),
        "running": s.running,
        "mode": s.mode,
        "credentials_set": credentials_present(),
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "last_tick_at": s.last_tick_at.isoformat() if s.last_tick_at else None,
        "trades_executed": s.trades_executed,
        "last_action": s.last_action,
        "last_error": s.last_error,
        "strategy_scan": storage.kv_get("profitstream_scan") or {},
        # ENTRY_HALTED (new BUYs blocked, e.g. drawdown breaker/emergency halt)
        # is distinct from SYSTEM_OFFLINE (the whole process/scheduler down).
        # Existing positions keep receiving stop-loss/TP/trailing/reconciliation
        # regardless of entry_halted — see Autopilot.tick().
        "entry_halted": bool(entry_status.get("entry_halted")),
        "entry_halted_reasons": entry_status.get("reasons") or [],
        "system_offline": bool(entry_status.get("system_offline", False)),
    }


@router.get("/config")
async def config_summary() -> dict:
    s = get_settings()
    return {
        "env": s.env,
        "live_mode": s.live_mode,
        "dry_run": s.dry_run,
        "paper_trading": s.paper_trading,
        "mode": s.trading_mode.value,
        "symbols": list(SYMBOLS),
        "timeframes": [tf.value for tf in TIMEFRAMES],
        "risk": {
            "max_position_pct": s.max_position_pct,
            "max_portfolio_risk_pct": s.max_portfolio_risk_pct,
            "kelly_fraction_cap": s.kelly_fraction_cap,
        },
    }


@router.get("/win-rate")
async def win_rate(mode: str | None = None) -> dict:
    """Win rate under BOTH definitions, clearly labelled.

    `position` is the target (legs grouped by symbol+entry_ts, fee-adjusted,
    book-keeping closes and still-open partials excluded). `row` is the legacy
    per-row number, inflated by partial TP legs — never compare it to the target.
    """
    if mode not in (None, "live", "paper"):
        raise HTTPException(status_code=400, detail="mode must be 'live' or 'paper'")
    sm = win_rate_summary_from_storage(mode=mode)
    return {
        "mode": mode or "all",
        "configured_definition": (
            "position" if get_settings().win_rate_position_level else "row"
        ),
        "position": {
            "win_rate": sm["position_win_rate"], "wins": sm["position_wins"], "n": sm["position_n"],
            "label": "POSITION-level, fee-adjusted (TARGET)",
        },
        "row": {
            "win_rate": sm["row_win_rate"], "wins": sm["row_wins"], "n": sm["row_n"],
            "label": "ROW-level, legacy (inflated by partial TP legs)",
        },
        "excluded": {
            "bookkeeping_rows": sm["excluded_bookkeeping_rows"],
            "open_partial_positions": sm["excluded_open_positions"],
        },
        "fee_flipped_positions": sm["fee_flipped_positions"],
        "note": sm["note"],
    }


@router.get("/metrics")
async def metrics() -> dict:
    """Operational metrics, including ML quality-gate stats.

    `gate.cumulative` accumulates across ticks since the gate was enabled;
    `gate.last_tick` is the most recent tick snapshot. `avg_win_prob` is the
    mean predicted win-probability over all evaluated BUY/SELL signals.
    """
    s = get_settings()
    raw = storage.kv_get("ml_gate_stats") or {}
    cum = raw.get("cumulative", {}) if isinstance(raw, dict) else {}
    evaluated = int(cum.get("evaluated", 0))
    proba_sum = float(cum.get("proba_sum", 0.0))
    return {
        "exchange_api": exchange_telemetry.snapshot(),
        "gate": {
            "enabled": s.ml_gate_enabled,
            "threshold": s.ml_gate_threshold,
            "model_version": raw.get("model_version") if isinstance(raw, dict) else None,
            "cumulative": {
                "evaluated": evaluated,
                "accepted": int(cum.get("accepted", 0)),
                "gated": int(cum.get("gated", 0)),
                "avg_win_prob": (proba_sum / evaluated) if evaluated else None,
            },
            "last_tick": raw.get("last_tick") if isinstance(raw, dict) else None,
        },
    }


@router.get("/live/diagnostics")
async def live_diagnostics() -> dict:
    health = storage.kv_get("health_status") or {}
    startup = storage.kv_get("startup_report") or {}
    audit = [
        dict(row, execution_stage=audit_labels.execution_stage(row))
        for row in storage.recent_trade_audit(limit=25, mode=get_settings().ledger_mode)
    ]
    return {
        "health": health,
        "startup_report": startup,
        "websocket": live_prices.status(),
        "trade_audit": audit,
    }
