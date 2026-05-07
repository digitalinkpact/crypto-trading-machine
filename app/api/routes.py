"""HTTP routes — read-only telemetry + manual triggers."""
from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse

from app.agents import run_all_agents
from app.config import SYMBOLS, TIMEFRAMES, get_settings
from app.signals import Signal

router = APIRouter()


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>AI Crypto Trading Machine</title>
  <style>
    * { box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
           margin: 0; background: #0b1020; color: #e6e8ef; }
    header { padding: 1.25rem 1.5rem; border-bottom: 1px solid #1d2540;
             display: flex; align-items: center; justify-content: space-between;
             flex-wrap: wrap; gap: 0.5rem; }
    header h1 { margin: 0; font-size: 1.15rem; letter-spacing: 0.02em; }
    header .sub { color: #8a93b2; font-size: 0.85rem; }
    main { padding: 1.25rem 1.5rem; display: grid; gap: 1rem;
           grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); }
    .card { background: #121831; border: 1px solid #1d2540; border-radius: 10px;
            padding: 1rem 1.1rem; }
    .card h2 { margin: 0 0 0.6rem; font-size: 0.95rem; color: #c8cee8;
               display: flex; justify-content: space-between; align-items: center; }
    .pill { font-size: 0.7rem; padding: 0.1rem 0.5rem; border-radius: 999px;
            background: #1d2540; color: #8a93b2; }
    .pill.ok { background: #0e3b27; color: #5be29a; }
    .pill.warn { background: #3b2a0e; color: #f0c45b; }
    table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
    th, td { padding: 0.4rem 0.5rem; text-align: left;
             border-bottom: 1px solid #1d2540; }
    th { color: #8a93b2; font-weight: 500; }
    .buy { color: #5be29a; font-weight: 600; }
    .sell { color: #ff7a8a; font-weight: 600; }
    .hold { color: #8a93b2; }
    .muted { color: #8a93b2; font-size: 0.85rem; }
    button { background: #2455e6; color: white; border: 0; border-radius: 6px;
             padding: 0.45rem 0.85rem; font-size: 0.85rem; cursor: pointer; }
    button:disabled { opacity: 0.6; cursor: wait; }
    a { color: #7aa6ff; text-decoration: none; }
    a:hover { text-decoration: underline; }
    code { background: #0b1020; padding: 0.05rem 0.35rem; border-radius: 4px;
           border: 1px solid #1d2540; font-size: 0.82rem; }
    .chips { display: flex; flex-wrap: wrap; gap: 0.35rem; }
    .chip { background: #1d2540; padding: 0.15rem 0.55rem; border-radius: 999px;
            font-size: 0.75rem; color: #c8cee8; }
    .row { display: flex; justify-content: space-between; padding: 0.25rem 0;
           border-bottom: 1px solid #1d2540; font-size: 0.85rem; }
    .row:last-child { border-bottom: 0; }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>AI Crypto Trading Machine</h1>
      <div class="sub">Binance.US · 7 agents · 25 coins · 4 timeframes</div>
    </div>
    <div>
      <a href="/docs">/docs</a> &nbsp;·&nbsp;
      <a href="/redoc">/redoc</a> &nbsp;·&nbsp;
      <a href="/config">/config</a>
    </div>
  </header>

  <main>
    <section class="card">
      <h2>Status <span id="health-pill" class="pill">checking…</span></h2>
      <div id="status-body" class="muted">Loading…</div>
    </section>

    <section class="card">
      <h2>Risk caps</h2>
      <div id="risk-body" class="muted">Loading…</div>
    </section>

    <section class="card" style="grid-column: 1 / -1;">
      <h2>
        Universe
        <span class="pill" id="universe-count">—</span>
      </h2>
      <div class="chips" id="symbols-chips"></div>
      <div class="muted" style="margin-top: 0.6rem;">
        Timeframes: <span id="tfs-chips"></span>
      </div>
    </section>

    <section class="card" style="grid-column: 1 / -1;">
      <h2>
        Aggregated signals
        <span>
          <label class="muted" style="margin-right: 0.5rem;">
            <input type="checkbox" id="use-llm" /> use LLM
          </label>
          <button id="tick-btn">Run tick</button>
        </span>
      </h2>
      <div id="signals-body" class="muted">
        Click <b>Run tick</b> to fan out all agents over every symbol/timeframe.
      </div>
    </section>
  </main>

  <script>
    const $ = (id) => document.getElementById(id);

    async function loadStatus() {
      try {
        const h = await fetch('/health').then(r => r.json());
        $('health-pill').textContent = h.status;
        $('health-pill').className = 'pill ' + (h.status === 'ok' ? 'ok' : 'warn');
      } catch {
        $('health-pill').textContent = 'down';
        $('health-pill').className = 'pill warn';
      }

      const c = await fetch('/config').then(r => r.json());
      $('status-body').innerHTML = `
        <div class="row"><span>Environment</span><b>${c.env}</b></div>
        <div class="row"><span>Dry-run</span><b>${c.dry_run}</b></div>
        <div class="row"><span>Paper trading</span><b>${c.paper_trading}</b></div>
      `;
      $('risk-body').innerHTML = `
        <div class="row"><span>Max position %</span><b>${(c.risk.max_position_pct*100).toFixed(1)}%</b></div>
        <div class="row"><span>Max portfolio risk %</span><b>${(c.risk.max_portfolio_risk_pct*100).toFixed(1)}%</b></div>
        <div class="row"><span>Kelly fraction cap</span><b>${(c.risk.kelly_fraction_cap*100).toFixed(1)}%</b></div>
      `;
      $('universe-count').textContent = c.symbols.length + ' symbols';
      $('symbols-chips').innerHTML = c.symbols
        .map(s => `<span class="chip">${s}</span>`).join('');
      $('tfs-chips').innerHTML = c.timeframes
        .map(t => `<span class="chip">${t}</span>`).join(' ');
    }

    async function runTick() {
      const btn = $('tick-btn');
      const useLlm = $('use-llm').checked;
      btn.disabled = true;
      btn.textContent = 'Running…';
      $('signals-body').innerHTML = '<div class="muted">Fanning out agents — this may take a moment…</div>';
      try {
        const res = await fetch('/agents/tick?use_llm=' + useLlm, { method: 'POST' });
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const data = await res.json();
        const symbols = Object.keys(data).sort();
        if (!symbols.length) {
          $('signals-body').innerHTML = '<div class="muted">No signals produced.</div>';
          return;
        }
        const rows = symbols.map(sym => {
          const s = data[sym];
          const cls = s.action.toLowerCase();
          const conf = (s.confidence * 100).toFixed(0) + '%';
          const rat = (s.rationale || '').slice(0, 180);
          return `<tr>
            <td><b>${sym}</b></td>
            <td class="${cls}">${s.action}</td>
            <td>${conf}</td>
            <td class="muted">${rat}</td>
          </tr>`;
        }).join('');
        $('signals-body').innerHTML = `
          <table>
            <thead><tr>
              <th>Symbol</th><th>Action</th><th>Confidence</th><th>Rationale</th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>`;
      } catch (e) {
        $('signals-body').innerHTML =
          '<div class="muted" style="color:#ff7a8a;">Tick failed: ' + e.message + '</div>';
      } finally {
        btn.disabled = false;
        btn.textContent = 'Run tick';
      }
    }

    $('tick-btn').addEventListener('click', runTick);
    loadStatus();
  </script>
</body>
</html>
"""


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index() -> str:
    return _INDEX_HTML


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


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


@router.post("/agents/tick")
async def agents_tick(use_llm: bool = Query(False)) -> dict[str, Signal]:
    """Run all agents once and return aggregated signals."""
    return await run_all_agents(use_llm=use_llm)
