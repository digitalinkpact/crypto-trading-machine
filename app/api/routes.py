"""HTTP routes — dashboard, settings, autopilot controls, trades log."""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import SYMBOLS, TIMEFRAMES, get_settings
from app.credentials import (
    credentials_present,
    save_binance_credentials,
    save_risk_settings,
    save_trading_mode,
)
from app.storage import storage
from app.trading import autopilot, portfolio_snapshot

router = APIRouter()


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
        f"<title>{title}</title><style>{_CSS}</style></head><body>"
        "<header><h1>AI Crypto Trading Machine</h1><nav>"
        + " &nbsp;&middot;&nbsp; ".join(nav)
        + f"</nav></header><main>{body}</main></body></html>"
    )


def _mode_pill() -> str:
    s = get_settings()
    if s.paper_trading:
        return "<span class='pill paper'>PAPER</span>"
    return "<span class='pill real'>LIVE &mdash; REAL MONEY</span>"


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> str:
    s = get_settings()
    is_paper = s.paper_trading
    creds = credentials_present()
    st = autopilot.state
    pill_class = "live" if st.running else "off"
    pill_text = "RUNNING" if st.running else "STOPPED"

    if is_paper:
        creds_banner = (
            "<div class='banner ok'>PAPER mode &mdash; trading with simulated $10,000. "
            "All trades, P&amp;L, and per-agent learning are saved and will carry over to LIVE.</div>"
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
    try:
        snap = await portfolio_snapshot()
        total = snap["total_usdt"]
        cash = snap["usdt_cash"]
        invested = total - cash
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

    stats = storage.agent_stats()
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
  <div class='muted'>Per-agent stats accumulate across PAPER and LIVE so the live system inherits everything.</div>
  <table style='margin-top:0.75rem;'>
    <thead><tr><th>Agent</th><th class='num'>Trades</th><th class='num'>Win rate</th><th class='num'>Total P&amp;L</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
</div>
        """
    else:
        agent_card = (
            "<div class='card'><h2>Agent learning</h2>"
            "<div class='muted'>No closed trades yet. Run autopilot in Paper mode "
            "to start building a track record.</div></div>"
        )

    # ── Risk events card ─────────────────────────────────────────────
    risk_events = storage.recent_risk_events(limit=10)
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
    curve = storage.equity_curve(limit=30)
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

    body = f"""
{creds_banner}
<div class='card'>
  <h2>Autopilot <span class='pill {pill_class}'>{pill_text}</span> {_mode_pill()}</h2>
  <div class='muted'>Trading runs automatically every 15 minutes once started.
    Stop will market-sell every coin back to USDT.</div>
  <div class='row'><span>Started</span><b>{started}</b></div>
  <div class='row'><span>Last tick</span><b>{last_tick}</b></div>
  <div class='row'><span>Trades executed (this run)</span><b>{st.trades_executed}</b></div>
  {last_err}
  <form method='post' action='/autopilot/start'>
    <button class='btn btn-start' {start_disabled}>&#9654;  Start auto-trading</button>
  </form>
  <form method='post' action='/autopilot/stop'
        onsubmit="return confirm('Stop autopilot and SELL all holdings to USDT?');">
    <button class='btn btn-stop' {stop_disabled}>&#9632;  Stop &amp; liquidate everything</button>
  </form>
</div>
{balance_card}
{equity_card}
{agent_card}
{risk_card}
<div class='card'>
  <h2>Universe</h2>
  <div class='muted'>{len(SYMBOLS)} symbols &middot; {len(TIMEFRAMES)} timeframes
    ({", ".join(t.value for t in TIMEFRAMES)})</div>
</div>
"""
    return _layout("Dashboard", body, active="home")


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
  <div class='muted'>PAPER simulates trades against live Binance.US prices &mdash;
    no real money. Every trade is saved so the LIVE system inherits the
    learning when you switch.</div>
  <form method='post' action='/settings/mode'>
    <label style='display:flex;align-items:center;gap:0.5rem;margin-top:1rem;'>
      <input type='radio' name='mode' value='paper' {paper_checked} />
      <span><b>Paper</b> &mdash; safe simulation (recommended)</span>
    </label>
    <label style='display:flex;align-items:center;gap:0.5rem;margin-top:0.5rem;'>
      <input type='radio' name='mode' value='live' {live_checked} />
      <span><b>Live</b> &mdash; real Binance.US orders, real money</span>
    </label>
    <button class='btn btn-mode' style='margin-top:1rem;'
      onclick="return confirm('Change trading mode? Stop autopilot first if it is running.');"
    >Save mode</button>
  </form>
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
    if mode not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be 'paper' or 'live'")
    save_trading_mode(paper=(mode == "paper"))
    autopilot.state.mode = mode
    return RedirectResponse(url="/settings?mode_saved=1", status_code=303)


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


@router.post("/autopilot/stop", include_in_schema=False)
async def autopilot_stop():
    await autopilot.stop_and_liquidate()
    return RedirectResponse(url="/", status_code=303)


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/autopilot/status")
async def autopilot_status() -> dict:
    s = autopilot.state
    return {
        "running": s.running,
        "mode": s.mode,
        "credentials_set": credentials_present(),
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "last_tick_at": s.last_tick_at.isoformat() if s.last_tick_at else None,
        "trades_executed": s.trades_executed,
        "last_action": s.last_action,
        "last_error": s.last_error,
    }


@router.get("/config")
async def config_summary() -> dict:
    s = get_settings()
    return {
        "env": s.env,
        "dry_run": s.dry_run,
        "paper_trading": s.paper_trading,
        "mode": "paper" if s.paper_trading else "live",
        "symbols": list(SYMBOLS),
        "timeframes": [tf.value for tf in TIMEFRAMES],
        "risk": {
            "max_position_pct": s.max_position_pct,
            "max_portfolio_risk_pct": s.max_portfolio_risk_pct,
            "kelly_fraction_cap": s.kelly_fraction_cap,
        },
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


