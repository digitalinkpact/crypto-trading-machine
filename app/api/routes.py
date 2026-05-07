"""HTTP routes — minimal dashboard, settings page, autopilot controls."""
from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import SYMBOLS, TIMEFRAMES, get_settings
from app.credentials import credentials_present, save_binance_credentials
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
main { max-width: 720px; margin: 0 auto; padding: 2rem 1.5rem; }
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
.btn { display: block; width: 100%; padding: 1rem; font-size: 1.05rem;
       font-weight: 600; border: 0; border-radius: 10px; cursor: pointer;
       margin-top: 0.5rem; color: white; }
.btn-start { background: #0e7a3f; }
.btn-start:hover { background: #109a4e; }
.btn-stop { background: #b3263a; }
.btn-stop:hover { background: #d52d44; }
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
form { margin: 0; }
"""


def _layout(title: str, body: str, active: str = "home") -> str:
    nav_home = "active" if active == "home" else ""
    nav_settings = "active" if active == "settings" else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'/>"
        f"<title>{title}</title><style>{_CSS}</style></head><body>"
        "<header><h1>AI Crypto Trading Machine</h1><nav>"
        f"<a class='{nav_home}' href='/'>Dashboard</a> &nbsp;·&nbsp; "
        f"<a class='{nav_settings}' href='/settings'>Settings</a>"
        f"</nav></header><main>{body}</main></body></html>"
    )


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> str:
    creds = credentials_present()
    st = autopilot.state
    pill_class = "live" if st.running else "off"
    pill_text = "RUNNING" if st.running else "STOPPED"

    creds_banner = "" if creds else (
        "<div class='banner warn'>No Binance.US API keys saved. "
        "<a href='/settings' style='color:#f0c45b;'>Add them on Settings</a> "
        "before starting.</div>"
    )

    # Try to fetch live portfolio. Fall back gracefully if no creds / API down.
    balance_card = ""
    if creds:
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
            ) or "<div class='row muted'><span>No holdings</span><span>—</span></div>"
            balance_card = f"""
    <div class='card'>
      <h2>Portfolio</h2>
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
                f"<div class='card'><h2>Portfolio</h2>"
                f"<div class='muted'>Could not fetch balance: {exc}</div></div>"
            )

    last_tick = st.last_tick_at.strftime("%Y-%m-%d %H:%M UTC") if st.last_tick_at else "—"
    started = st.started_at.strftime("%Y-%m-%d %H:%M UTC") if st.started_at else "—"
    last_err = (
        f"<div class='row'><span>Last error</span>"
        f"<b style='color:#ff7a8a;'>{st.last_error}</b></div>"
        if st.last_error else ""
    )

    start_disabled = "" if (creds and not st.running) else "disabled"
    stop_disabled = "" if st.running else "disabled"

    body = f"""
    {creds_banner}
    <div class='card'>
      <h2>Autopilot <span class='pill {pill_class}'>{pill_text}</span></h2>
      <div class='muted'>Trading runs automatically every 15 minutes once started.
        Stop will market-sell every coin back to USDT.</div>
      <div class='row'><span>Started</span><b>{started}</b></div>
      <div class='row'><span>Last tick</span><b>{last_tick}</b></div>
      <div class='row'><span>Trades executed</span><b>{st.trades_executed}</b></div>
      {last_err}
      <form method='post' action='/autopilot/start'>
        <button class='btn btn-start' {start_disabled}>▶  Start auto-trading</button>
      </form>
      <form method='post' action='/autopilot/stop'
            onsubmit="return confirm('Stop autopilot and SELL all holdings to USDT?');">
        <button class='btn btn-stop' {stop_disabled}>■  Stop &amp; liquidate everything</button>
      </form>
    </div>
    {balance_card}
    <div class='card'>
      <h2>Universe</h2>
      <div class='muted'>{len(SYMBOLS)} symbols · {len(TIMEFRAMES)} timeframes
        ({", ".join(t.value for t in TIMEFRAMES)})</div>
    </div>
    """
    return _layout("Dashboard", body, active="home")


@router.get("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_page(saved: int = 0) -> str:
    creds = credentials_present()
    saved_banner = "<div class='banner ok'>API keys saved.</div>" if saved else ""
    status = "API keys are set." if creds else "No API keys saved yet."

    body = f"""
    {saved_banner}
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
        "symbols": list(SYMBOLS),
        "timeframes": [tf.value for tf in TIMEFRAMES],
        "risk": {
            "max_position_pct": s.max_position_pct,
            "max_portfolio_risk_pct": s.max_portfolio_risk_pct,
            "kelly_fraction_cap": s.kelly_fraction_cap,
        },
    }

