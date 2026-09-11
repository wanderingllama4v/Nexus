"""
api/main.py — NEXUS FastAPI server + web dashboard (Phase 5)

Endpoints:
  GET  /              → web dashboard
  GET  /health        → {"status":"ok"}
  POST /run           → trigger pipeline run
  GET  /status        → running state
  GET  /runs          → list recent runs
  GET  /runs/{id}     → run detail (scans + contracts + agents)
  GET  /runs/{id}/brief   → ANALYST brief for a run
  GET  /runs/{id}/agents  → all agent outputs for a run
  GET  /trades/open   → open positions
  GET  /trades/history    → closed trade history
  GET  /trades/summary    → win rate + total P&L
  POST /execute/{id}  → place orders for GUARDIAN APPROVED trades
  POST /monitor       → check open positions vs stop/target
"""

import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from shared import db
from shared.config import SCAN_UNIVERSE
import nexus as nexus_pipeline
import components.executor as executor
import components.monitor as monitor

_IS_PAPER = os.getenv("TT_PAPER", "true").lower() == "true"


async def _monitor_loop():
    """Check open positions every 60s. Runs in background for the life of the server."""
    while True:
        await asyncio.sleep(60)
        try:
            if db.get_open_trades():
                await asyncio.to_thread(monitor.check_all, _IS_PAPER)
        except Exception as e:
            print(f"[monitor-loop] {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_monitor_loop())
    yield
    task.cancel()


app = FastAPI(title="NEXUS", version="5.0.0", lifespan=lifespan)


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "phase": 5, "ts": datetime.utcnow().isoformat()}


# ── Pipeline trigger ───────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    symbols:  Optional[list[str]] = None
    phase1:   bool = False
    phase2:   bool = False
    phase3:   bool = False


_active_run: dict = {"run_id": None, "running": False}


def _background_run(req: RunRequest):
    _active_run["running"] = True
    try:
        run_id = nexus_pipeline.run_pipeline(
            symbols=req.symbols,
            phase1_only=req.phase1,
            phase2_only=req.phase2,
            phase3_only=req.phase3,
        )
        _active_run["run_id"] = run_id
    finally:
        _active_run["running"] = False


@app.post("/run")
def trigger_run(req: RunRequest, background_tasks: BackgroundTasks):
    if _active_run["running"]:
        return {"status": "already_running", "run_id": _active_run["run_id"]}
    background_tasks.add_task(_background_run, req)
    return {"status": "started"}


@app.get("/status")
def run_status():
    return {"running": _active_run["running"], "last_run_id": _active_run["run_id"]}


# ── Run data ───────────────────────────────────────────────────────────────────

@app.get("/runs")
def list_runs(limit: int = 20):
    with db.cursor() as cur:
        cur.execute(
            """SELECT id, started_at, completed_at, status, phase,
                      symbols_scanned, contracts_found, market_regime
               FROM runs ORDER BY id DESC LIMIT %s""",
            (limit,),
        )
        return cur.fetchall()


@app.get("/runs/{run_id}")
def get_run(run_id: int):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM runs WHERE id = %s", (run_id,))
        run = cur.fetchone()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return {
        "run":          run,
        "symbol_scans": db.get_symbol_scans(run_id),
        "contracts":    db.get_contract_scans(run_id),
    }


@app.get("/runs/{run_id}/brief")
def get_run_brief(run_id: int):
    """Return the ANALYST brief for a run."""
    row = db.get_agent_output(run_id, "analyst")
    if not row:
        raise HTTPException(status_code=404, detail="No ANALYST output for this run")
    try:
        d = row["output_data"]
        return json.loads(d) if isinstance(d, str) else d
    except Exception:
        return {"error": "Could not parse analyst output"}


@app.get("/runs/{run_id}/agents")
def get_run_agents(run_id: int):
    """Return all agent outputs for a run, parsed."""
    rows = db.get_agent_outputs(run_id)
    result = {}
    for row in rows:
        agent = row["agent"]
        try:
            d = row["output_data"]
            data = json.loads(d) if isinstance(d, str) else d
        except Exception:
            data = row["output_data"]
        result[agent] = {
            "data":       data,
            "score":      row["score"],
            "tokens_in":  row["tokens_in"],
            "tokens_out": row["tokens_out"],
            "latency_ms": row["latency_ms"],
            "model":      row["model"],
            "created_at": str(row["created_at"]),
        }
    return result


@app.get("/runs/{run_id}/symbols")
def get_run_symbols(run_id: int):
    return db.get_symbol_scans(run_id)


@app.get("/runs/{run_id}/contracts")
def get_run_contracts(run_id: int, symbol: str = None):
    return db.get_contract_scans(run_id, symbol=symbol)


# ── Trades ─────────────────────────────────────────────────────────────────────

@app.get("/trades/open")
def get_open_trades():
    return db.get_open_trades()


@app.get("/trades/history")
def get_trade_history(limit: int = 50):
    return db.get_trade_history(limit=limit)


@app.get("/trades/summary")
def get_trade_summary():
    return monitor.summary()


@app.get("/trades/{trade_id}")
def get_trade(trade_id: int):
    t = db.get_trade(trade_id)
    if not t:
        raise HTTPException(status_code=404, detail="Trade not found")
    return t


# ── Execution ─────────────────────────────────────────────────────────────────

@app.post("/execute/{run_id}")
def execute_run(run_id: int, live: bool = False):
    """
    Execute GUARDIAN APPROVED trades for a run.
    live=false (default): record to DB as pending, no real orders.
    live=true: place orders via Tastytrade.
    """
    trades = executor.run(run_id, dry_run=not live)
    return {"executed": len(trades), "trades": trades, "live": live}


@app.post("/monitor")
def check_positions(auto_close: bool = False):
    """Check open positions vs stop/target. auto_close only on paper accounts."""
    results = monitor.check_all(auto_close=auto_close)
    return {"checked": len(results), "results": results}


# ── Web dashboard ──────────────────────────────────────────────────────────────

DASHBOARD = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NEXUS</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:'SF Mono','Fira Code',monospace;background:#0d1117;color:#e6edf3;font-size:13px}
  header{background:#161b22;border-bottom:1px solid #30363d;padding:12px 24px;display:flex;align-items:center;gap:16px}
  header h1{font-size:18px;font-weight:700;color:#58a6ff;letter-spacing:2px}
  header span{color:#8b949e;font-size:11px}
  .badge{background:#1f6feb;color:#fff;font-size:10px;padding:2px 8px;border-radius:10px}
  main{padding:24px;max-width:1400px;margin:0 auto}
  .tabs{display:flex;gap:4px;margin-bottom:24px;border-bottom:1px solid #21262d;padding-bottom:0}
  .tab{padding:8px 16px;cursor:pointer;color:#8b949e;border-bottom:2px solid transparent;margin-bottom:-1px}
  .tab.active{color:#e6edf3;border-bottom-color:#58a6ff}
  .tab:hover{color:#e6edf3}
  .panel{display:none}.panel.active{display:block}
  .controls{display:flex;gap:12px;margin-bottom:24px;align-items:center;flex-wrap:wrap}
  button{background:#238636;color:#fff;border:none;padding:8px 18px;border-radius:6px;cursor:pointer;font-family:inherit;font-size:12px}
  button:hover{background:#2ea043}
  button.secondary{background:#21262d;border:1px solid #30363d;color:#e6edf3}
  button.secondary:hover{background:#30363d}
  button.danger{background:#da3633}
  button.danger:hover{background:#f85149}
  #status{color:#8b949e;font-size:12px}
  .section{margin-bottom:28px}
  .section h2{font-size:12px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:10px}
  table{width:100%;border-collapse:collapse}
  th{text-align:left;padding:7px 10px;color:#8b949e;font-size:11px;text-transform:uppercase;border-bottom:1px solid #21262d;white-space:nowrap}
  td{padding:7px 10px;border-bottom:1px solid #161b22;white-space:nowrap}
  tr:hover td{background:#161b22}
  .bullish{color:#3fb950}.bearish{color:#f85149}.neutral{color:#8b949e}
  .score-hi{color:#3fb950;font-weight:700}.score-md{color:#d29922}.score-lo{color:#8b949e}
  .tag{display:inline-block;padding:1px 6px;border-radius:4px;font-size:10px}
  .tag-completed{background:#033a16;color:#3fb950}
  .tag-running{background:#1a3f6f;color:#58a6ff}
  .tag-failed{background:#3d1111;color:#f85149}
  .tag-pending{background:#1c1f24;color:#8b949e}
  .tag-open{background:#033a16;color:#3fb950}
  .tag-closed{background:#21262d;color:#8b949e}
  .approved{color:#3fb950;font-weight:700}.rejected{color:#f85149}
  .brief-box{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;white-space:pre-wrap;line-height:1.6;color:#c9d1d9;font-size:12px;margin-bottom:16px}
  .headline{font-size:15px;font-weight:700;color:#58a6ff;margin-bottom:12px}
  .regime-badge{display:inline-block;padding:3px 10px;border-radius:12px;font-size:11px;font-weight:700;margin-right:8px}
  .regime-bullish{background:#033a16;color:#3fb950}
  .regime-bearish{background:#3d1111;color:#f85149}
  .regime-choppy{background:#272115;color:#d29922}
  .regime-risk{background:#3d1111;color:#f85149}
  .run-row{cursor:pointer}.run-row:hover td{background:#1f2937}
  .back{color:#58a6ff;cursor:pointer;font-size:12px;margin-bottom:16px;display:inline-block}
  .back:hover{text-decoration:underline}
  .contract-group{margin-bottom:20px}
  .contract-group h3{font-size:13px;color:#e6edf3;margin-bottom:8px;border-left:3px solid #58a6ff;padding-left:8px}
  .pnl-pos{color:#3fb950;font-weight:700}.pnl-neg{color:#f85149;font-weight:700}
  .actions-list{list-style:none;padding:0}
  .actions-list li{padding:4px 0;padding-left:12px;border-left:2px solid #58a6ff;margin-bottom:6px;color:#c9d1d9;font-size:12px}
  .risk-list li{border-left-color:#f85149}
  select{background:#21262d;color:#e6edf3;border:1px solid #30363d;padding:6px 10px;border-radius:6px;font-family:inherit;font-size:12px}
</style>
</head>
<body>
<header>
  <h1>NEXUS</h1>
  <span>Multi-AI Trading Orchestrator</span>
  <span class="badge">Phase 5</span>
</header>
<main>

<div class="tabs">
  <div class="tab active" onclick="showTab('dashboard')">Dashboard</div>
  <div class="tab" onclick="showTab('runs')">Runs</div>
  <div class="tab" onclick="showTab('trades')">Trades</div>
  <div class="tab" onclick="showTab('detail')" id="detail-tab" style="display:none">Run Detail</div>
</div>

<!-- DASHBOARD TAB -->
<div class="panel active" id="panel-dashboard">
  <div class="controls">
    <select id="run-mode">
      <option value="">Full Pipeline (Phase 4)</option>
      <option value="phase3">Phase 3 (no risk layer)</option>
      <option value="phase2">Phase 2 (scan only)</option>
      <option value="phase1">Phase 1 (no AI)</option>
    </select>
    <button onclick="triggerRun()">&#9654; Run NEXUS</button>
    <button class="secondary" onclick="loadDashboard()">&#8635; Refresh</button>
    <button class="secondary" onclick="checkPositions()">&#128270; Check Positions</button>
    <span id="status"></span>
  </div>
  <div id="dashboard-content"><div style="color:#8b949e">Loading...</div></div>
</div>

<!-- RUNS TAB -->
<div class="panel" id="panel-runs">
  <div class="controls">
    <button class="secondary" onclick="loadRuns()">&#8635; Refresh</button>
  </div>
  <div class="section">
    <h2>Recent Runs</h2>
    <table>
      <thead><tr>
        <th>ID</th><th>Started</th><th>Duration</th><th>Status</th>
        <th>Phase</th><th>Regime</th><th>Symbols</th><th>Contracts</th>
      </tr></thead>
      <tbody id="runs-body"></tbody>
    </table>
  </div>
</div>

<!-- TRADES TAB -->
<div class="panel" id="panel-trades">
  <div class="controls">
    <button class="secondary" onclick="loadTrades()">&#8635; Refresh</button>
    <button class="secondary" onclick="checkPositions()">&#128270; Check Positions</button>
  </div>
  <div class="section">
    <h2>Open Positions</h2>
    <table>
      <thead><tr>
        <th>ID</th><th>Symbol</th><th>Contract</th><th>Type</th>
        <th>Qty</th><th>Entry</th><th>Stop</th><th>Target</th><th>Status</th>
      </tr></thead>
      <tbody id="open-trades-body"></tbody>
    </table>
  </div>
  <div class="section">
    <h2>Trade History</h2>
    <div id="summary-bar" style="margin-bottom:10px;color:#8b949e;font-size:12px"></div>
    <table>
      <thead><tr>
        <th>ID</th><th>Symbol</th><th>Contract</th><th>Entry</th><th>Exit</th>
        <th>P&L</th><th>P&L%</th><th>Exit Reason</th><th>Closed</th>
      </tr></thead>
      <tbody id="history-body"></tbody>
    </table>
  </div>
</div>

<!-- RUN DETAIL (hidden tab) -->
<div class="panel" id="panel-detail">
  <span class="back" onclick="backToRuns()">&#8592; Back to Runs</span>
  <div id="detail-content"></div>
</div>

</main>

<script>
const API = '';

function showTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.getElementById('panel-'+name).classList.add('active');
  const tabs = document.querySelectorAll('.tab');
  const idx = ['dashboard','runs','trades','detail'].indexOf(name);
  if (idx >= 0 && tabs[idx]) tabs[idx].classList.add('active');
}

// ── Dashboard ──────────────────────────────────────────────────────────────────

async function loadDashboard() {
  const el = document.getElementById('dashboard-content');
  try {
    const runs = await fetch('/runs?limit=1').then(r => r.json());
    if (!runs.length) { el.innerHTML = '<p style="color:#8b949e">No runs yet. Click Run NEXUS to start.</p>'; return; }
    const latestId = runs[0].id;
    const [runData, brief, agents] = await Promise.all([
      fetch(`/runs/${latestId}`).then(r => r.json()),
      fetch(`/runs/${latestId}/brief`).then(r => r.json()).catch(() => null),
      fetch(`/runs/${latestId}/agents`).then(r => r.json()).catch(() => ({})),
    ]);
    const run = runData.run;

    let html = '';

    // Regime + meta
    const regime = run.market_regime || '';
    const regimeClass = regime.includes('BULL') ? 'regime-bullish' : regime.includes('BEAR') || regime.includes('RISK') ? 'regime-bearish' : 'regime-choppy';
    const dur = run.completed_at ? Math.round((new Date(run.completed_at) - new Date(run.started_at))/1000) + 's' : 'running...';
    html += `<div style="margin-bottom:16px">
      ${regime ? `<span class="regime-badge ${regimeClass}">${regime}</span>` : ''}
      <span style="color:#8b949e;font-size:12px">Run #${run.id} &bull; Phase ${run.phase} &bull; ${new Date(run.started_at).toLocaleString()} &bull; ${dur}</span>
      <button class="secondary" style="margin-left:12px;font-size:11px" onclick="loadRunDetail(${run.id})">View Detail</button>
    </div>`;

    // ANALYST brief
    if (brief && brief.headline) {
      html += `<div class="section">
        <h2>ANALYST Brief</h2>
        <div class="headline">${brief.headline || ''}</div>`;
      if (brief.brief) html += `<div class="brief-box">${escHtml(brief.brief)}</div>`;
      if (brief.action_items?.length) {
        html += `<h2 style="margin-top:12px">Action Items</h2><ul class="actions-list">`;
        brief.action_items.forEach(a => html += `<li>${escHtml(a)}</li>`);
        html += '</ul>';
      }
      if (brief.risk_factors?.length) {
        html += `<h2 style="margin-top:12px">Risk Factors</h2><ul class="actions-list risk-list">`;
        brief.risk_factors.forEach(r => html += `<li>${escHtml(r)}</li>`);
        html += '</ul>';
      }
      html += '</div>';
    }

    // GUARDIAN decisions
    const guardianData = agents['guardian'];
    if (guardianData?.data?.final_decisions?.length) {
      const decisions = guardianData.data.final_decisions;
      html += `<div class="section"><h2>GUARDIAN Decisions (${guardianData.data.approved_count || 0} approved)</h2><table>
        <thead><tr><th>Verdict</th><th>Contract</th><th>Qty</th><th>Entry</th><th>Stop</th><th>Target</th><th>Risk</th><th>Reason</th></tr></thead><tbody>`;
      decisions.forEach(d => {
        const cls = d.verdict === 'APPROVED' ? 'approved' : 'rejected';
        html += `<tr>
          <td class="${cls}">${d.verdict}</td>
          <td>${d.contract_symbol}</td>
          <td>${d.contracts ?? '—'}</td>
          <td>${d.entry_price != null ? '$'+d.entry_price.toFixed(2) : '—'}</td>
          <td>${d.stop_loss    != null ? '$'+d.stop_loss.toFixed(2)  : '—'}</td>
          <td>${d.profit_target_1 != null ? '$'+d.profit_target_1.toFixed(2) : '—'}</td>
          <td>${d.risk_pct != null ? d.risk_pct.toFixed(2)+'%' : '—'}</td>
          <td style="color:#8b949e;font-size:11px">${escHtml(d.rejection_reason || '')}</td>
        </tr>`;
      });
      html += '</tbody></table>';
      const approvedCount = decisions.filter(d => d.verdict === 'APPROVED').length;
      if (approvedCount > 0) {
        html += `<div style="margin-top:10px">
          <button onclick="executeRun(${run.id}, false)">&#9654; Record Trades (dry run)</button>
          <button class="danger" style="margin-left:8px" onclick="executeRun(${run.id}, true)">&#9889; Execute Live</button>
        </div>`;
      }
      html += '</div>';
    }

    // SENTINEL
    const sentinelData = agents['sentinel'];
    if (sentinelData?.data) {
      const s = sentinelData.data;
      html += `<div class="section"><h2>SENTINEL</h2>
        <span>Portfolio risk: <b style="color:${s.portfolio_risk==='LOW'?'#3fb950':s.portfolio_risk==='CRITICAL'?'#f85149':'#d29922'}">${s.portfolio_risk}</b></span>
        &nbsp;|&nbsp; net_liq: <b>$${(s.net_liq||0).toLocaleString()}</b>
        &nbsp;|&nbsp; buying_power: <b>$${(s.buying_power||0).toLocaleString()}</b>
        ${s.risk_flags?.length ? `<br><span style="color:#d29922">Flags: ${s.risk_flags.join(', ')}</span>` : ''}
      </div>`;
    }

    el.innerHTML = html;
  } catch(e) {
    el.innerHTML = `<p style="color:#f85149">Error: ${e.message}</p>`;
  }
}

async function executeRun(runId, live) {
  if (live && !confirm('Execute LIVE orders via Tastytrade? This places real trades.')) return;
  const res = await fetch(`/execute/${runId}?live=${live}`, {method:'POST'});
  const data = await res.json();
  alert(`Executor: ${data.executed} trade(s) ${live ? 'placed' : 'recorded (dry run)'}`);
  loadTrades();
}

async function checkPositions() {
  document.getElementById('status').textContent = 'Checking positions...';
  const res = await fetch('/monitor', {method:'POST'});
  const data = await res.json();
  document.getElementById('status').textContent = `Checked ${data.checked} position(s)`;
}

// ── Runs ──────────────────────────────────────────────────────────────────────

async function loadRuns() {
  const res = await fetch('/runs?limit=30');
  const runs = await res.json();
  const body = document.getElementById('runs-body');
  body.innerHTML = '';
  for (const r of runs) {
    const dur = r.completed_at ? Math.round((new Date(r.completed_at)-new Date(r.started_at))/1000)+'s' : '—';
    const tc = r.status==='completed'?'tag-completed':r.status==='running'?'tag-running':'tag-failed';
    body.innerHTML += `<tr class="run-row" onclick="loadRunDetail(${r.id})">
      <td>#${r.id}</td>
      <td>${new Date(r.started_at).toLocaleString()}</td>
      <td>${dur}</td>
      <td><span class="tag ${tc}">${r.status}</span></td>
      <td>${r.phase}</td>
      <td style="color:#8b949e;font-size:11px">${r.market_regime||'—'}</td>
      <td>${r.symbols_scanned||'—'}</td>
      <td>${r.contracts_found||'—'}</td>
    </tr>`;
  }
}

async function loadRunDetail(runId) {
  document.getElementById('detail-tab').style.display = '';
  showTab('detail');
  const [runData, brief, agents] = await Promise.all([
    fetch(`/runs/${runId}`).then(r=>r.json()),
    fetch(`/runs/${runId}/brief`).then(r=>r.json()).catch(()=>null),
    fetch(`/runs/${runId}/agents`).then(r=>r.json()).catch(()=>({})),
  ]);
  const run = runData.run;
  let html = `<h2 style="margin-bottom:16px;color:#8b949e">Run #${runId} &bull; Phase ${run.phase} &bull; ${run.market_regime||''}</h2>`;

  // Brief
  if (brief?.headline) {
    html += `<div class="section"><div class="headline">${brief.headline}</div>`;
    if (brief.brief) html += `<div class="brief-box">${escHtml(brief.brief)}</div>`;
    html += '</div>';
  }

  // Symbol scans
  if (runData.symbol_scans?.length) {
    html += '<div class="section"><h2>Symbol Scans</h2><table><thead><tr><th>Symbol</th><th>Score</th><th>Direction</th><th>RSI</th><th>VWAP</th><th>EMA</th><th>Vol</th><th>RS</th></tr></thead><tbody>';
    for (const s of runData.symbol_scans) {
      const dc = s.direction==='BULLISH'?'bullish':s.direction==='BEARISH'?'bearish':'neutral';
      const sc = s.technical_score>=65?'score-hi':s.technical_score>=45?'score-md':'score-lo';
      html += `<tr><td><b>${s.symbol}</b></td><td class="${sc}">${s.technical_score}</td>
        <td class="${dc}">${s.direction}</td><td>${s.rsi||'—'}</td>
        <td>${s.vwap_position==='ABOVE'?'▲':'▼'}</td>
        <td>${s.ema_aligned?'✓✓✓':s.ema9>s.ema21?'✓✓':'✗'}</td>
        <td>${s.volume_ratio||'—'}x</td>
        <td>${s.relative_strength!=null?s.relative_strength+'%':'—'}</td></tr>`;
    }
    html += '</tbody></table></div>';
  }

  // Contracts grouped by symbol
  if (runData.contracts?.length) {
    html += '<div class="section"><h2>Contracts</h2>';
    const bySymbol = {};
    for (const c of runData.contracts) { if (!bySymbol[c.symbol]) bySymbol[c.symbol]=[]; bySymbol[c.symbol].push(c); }
    for (const [sym, cs] of Object.entries(bySymbol)) {
      html += `<div class="contract-group"><h3>${sym}</h3><table><thead><tr>
        <th>Contract</th><th>DTE</th><th>Strike</th><th>Δ</th><th>IV</th><th>OI</th><th>Vol</th><th>Spread</th><th>Score</th>
        </tr></thead><tbody>`;
      for (const c of cs) {
        const sc = c.contract_score>=70?'score-hi':c.contract_score>=50?'score-md':'score-lo';
        html += `<tr><td>${c.contract_symbol}</td><td>${c.dte}</td><td>$${c.strike}</td>
          <td>${c.delta}</td><td>${(c.iv*100).toFixed(1)}%</td>
          <td>${c.open_interest?.toLocaleString()||'—'}</td>
          <td>${c.day_volume?.toLocaleString()||'—'}</td>
          <td>${c.spread_pct}%</td><td class="${sc}">${c.contract_score}</td></tr>`;
      }
      html += '</tbody></table></div>';
    }
    html += '</div>';
  }

  document.getElementById('detail-content').innerHTML = html;
}

function backToRuns() {
  document.getElementById('detail-tab').style.display = 'none';
  showTab('runs');
}

// ── Trades ─────────────────────────────────────────────────────────────────────

async function loadTrades() {
  const [open, history, summary] = await Promise.all([
    fetch('/trades/open').then(r=>r.json()),
    fetch('/trades/history?limit=50').then(r=>r.json()),
    fetch('/trades/summary').then(r=>r.json()),
  ]);

  const ob = document.getElementById('open-trades-body');
  ob.innerHTML = '';
  for (const t of open) {
    const tc = t.status==='open'?'tag-open':'tag-pending';
    ob.innerHTML += `<tr>
      <td>#${t.id}</td><td><b>${t.symbol}</b></td><td style="font-size:11px">${t.contract_symbol}</td>
      <td>${t.option_type}</td><td>${t.contracts}</td>
      <td>$${parseFloat(t.entry_price||0).toFixed(2)}</td>
      <td>$${parseFloat(t.stop_price||0).toFixed(2)}</td>
      <td>$${parseFloat(t.target_price||0).toFixed(2)}</td>
      <td><span class="tag ${tc}">${t.status}</span></td>
    </tr>`;
  }

  const sb = document.getElementById('summary-bar');
  sb.textContent = `${summary.trades} trades | Win rate: ${summary.win_rate}% | Total P&L: $${summary.total_pnl?.toFixed(2)||0} | W:${summary.winners} L:${summary.losers}`;

  const hb = document.getElementById('history-body');
  hb.innerHTML = '';
  for (const t of history) {
    const pnl = parseFloat(t.pnl||0);
    const pnlCls = pnl > 0 ? 'pnl-pos' : 'pnl-neg';
    hb.innerHTML += `<tr>
      <td>#${t.id}</td><td><b>${t.symbol}</b></td><td style="font-size:11px">${t.contract_symbol}</td>
      <td>$${parseFloat(t.entry_price||0).toFixed(2)}</td>
      <td>$${parseFloat(t.exit_price||0).toFixed(2)}</td>
      <td class="${pnlCls}">$${pnl.toFixed(2)}</td>
      <td class="${pnlCls}">${parseFloat(t.pnl_pct||0).toFixed(2)}%</td>
      <td style="color:#8b949e;font-size:11px">${t.exit_reason||'—'}</td>
      <td style="color:#8b949e">${t.closed_at ? new Date(t.closed_at).toLocaleDateString() : '—'}</td>
    </tr>`;
  }
}

// ── Trigger ───────────────────────────────────────────────────────────────────

async function triggerRun() {
  const mode = document.getElementById('run-mode').value;
  const body = { phase1: mode==='phase1', phase2: mode==='phase2', phase3: mode==='phase3' };
  document.getElementById('status').textContent = '⚡ Starting...';
  await fetch('/run', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
  setTimeout(() => { pollStatus(); loadDashboard(); }, 1000);
}

async function pollStatus() {
  const res = await fetch('/status');
  const s = await res.json();
  document.getElementById('status').textContent = s.running ? '⚡ Running pipeline...' : '';
  if (s.running) setTimeout(pollStatus, 4000);
  else loadDashboard();
}

function escHtml(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// Init
loadDashboard();
loadRuns();
loadTrades();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD
