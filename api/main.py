"""
api/main.py — NEXUS FastAPI server + web dashboard

Endpoints:
  GET  /              → web dashboard (HTML)
  GET  /health        → {"status": "ok"}
  POST /run           → trigger a new pipeline run
  GET  /runs          → list recent runs
  GET  /runs/{id}     → run detail with symbol scans + contracts

Run:
  uvicorn api.main:app --host 0.0.0.0 --port 8001 --reload
"""

import threading
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from shared import db
from shared.config import SCAN_UNIVERSE
import nexus as nexus_pipeline

app = FastAPI(title="NEXUS", version="1.0.0-phase1")


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "phase": 1, "ts": datetime.utcnow().isoformat()}


# ── Trigger run ───────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    symbols: Optional[list[str]] = None


_active_run: dict = {"run_id": None, "running": False}


def _background_run(symbols):
    _active_run["running"] = True
    try:
        run_id = nexus_pipeline.run_pipeline(symbols=symbols)
        _active_run["run_id"] = run_id
    finally:
        _active_run["running"] = False


@app.post("/run")
def trigger_run(req: RunRequest, background_tasks: BackgroundTasks):
    if _active_run["running"]:
        return {"status": "already_running", "run_id": _active_run["run_id"]}
    background_tasks.add_task(_background_run, req.symbols or SCAN_UNIVERSE)
    return {"status": "started"}


@app.get("/status")
def run_status():
    return {"running": _active_run["running"], "last_run_id": _active_run["run_id"]}


# ── Run data ──────────────────────────────────────────────────────────────────

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
    scans     = db.get_symbol_scans(run_id)
    contracts = db.get_contract_scans(run_id)
    return {"run": run, "symbol_scans": scans, "contracts": contracts}


@app.get("/runs/{run_id}/symbols")
def get_run_symbols(run_id: int):
    return db.get_symbol_scans(run_id)


@app.get("/runs/{run_id}/contracts")
def get_run_contracts(run_id: int, symbol: str = None):
    return db.get_contract_scans(run_id, symbol=symbol)


# ── Web dashboard ─────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>NEXUS</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: 'SF Mono', 'Fira Code', monospace; background: #0d1117; color: #e6edf3; font-size: 13px; }
  header { background: #161b22; border-bottom: 1px solid #30363d; padding: 12px 24px; display: flex; align-items: center; gap: 16px; }
  header h1 { font-size: 18px; font-weight: 700; color: #58a6ff; letter-spacing: 2px; }
  header span { color: #8b949e; font-size: 11px; }
  .badge { background: #1f6feb; color: #fff; font-size: 10px; padding: 2px 8px; border-radius: 10px; }
  main { padding: 24px; max-width: 1400px; margin: 0 auto; }
  .controls { display: flex; gap: 12px; margin-bottom: 24px; align-items: center; }
  button { background: #238636; color: #fff; border: none; padding: 8px 20px; border-radius: 6px; cursor: pointer; font-family: inherit; font-size: 13px; }
  button:hover { background: #2ea043; }
  button.secondary { background: #21262d; border: 1px solid #30363d; }
  button.secondary:hover { background: #30363d; }
  #status { color: #8b949e; font-size: 12px; }
  .section { margin-bottom: 32px; }
  .section h2 { font-size: 14px; color: #8b949e; text-transform: uppercase; letter-spacing: 1px; margin-bottom: 12px; }
  table { width: 100%; border-collapse: collapse; }
  th { text-align: left; padding: 8px 12px; color: #8b949e; font-size: 11px; text-transform: uppercase; border-bottom: 1px solid #21262d; white-space: nowrap; }
  td { padding: 8px 12px; border-bottom: 1px solid #161b22; white-space: nowrap; }
  tr:hover td { background: #161b22; }
  .bullish { color: #3fb950; }
  .bearish { color: #f85149; }
  .neutral  { color: #8b949e; }
  .score-high { color: #3fb950; font-weight: 700; }
  .score-mid  { color: #d29922; }
  .score-low  { color: #8b949e; }
  .run-row { cursor: pointer; }
  .run-row:hover td { background: #1f2937; }
  .tag { display: inline-block; padding: 1px 6px; border-radius: 4px; font-size: 10px; }
  .tag-completed { background: #033a16; color: #3fb950; }
  .tag-running   { background: #1a3f6f; color: #58a6ff; }
  .tag-failed    { background: #3d1111; color: #f85149; }
  #detail { display: none; }
  .back { color: #58a6ff; cursor: pointer; font-size: 12px; margin-bottom: 16px; display: inline-block; }
  .back:hover { text-decoration: underline; }
  .contract-group { margin-bottom: 20px; }
  .contract-group h3 { font-size: 13px; color: #e6edf3; margin-bottom: 8px; border-left: 3px solid #58a6ff; padding-left: 8px; }
</style>
</head>
<body>
<header>
  <h1>NEXUS</h1>
  <span>Multi-AI Trading Orchestrator</span>
  <span class="badge">Phase 1</span>
</header>
<main>

<div id="list-view">
  <div class="controls">
    <button onclick="triggerRun()">&#9654; Run Scan</button>
    <button class="secondary" onclick="loadRuns()">&#8635; Refresh</button>
    <span id="status"></span>
  </div>

  <div class="section">
    <h2>Recent Runs</h2>
    <table>
      <thead>
        <tr>
          <th>ID</th><th>Started</th><th>Duration</th><th>Status</th>
          <th>Phase</th><th>Symbols</th><th>Contracts</th>
        </tr>
      </thead>
      <tbody id="runs-body"></tbody>
    </table>
  </div>
</div>

<div id="detail-view" style="display:none">
  <span class="back" onclick="showList()">&#8592; Back to runs</span>
  <div class="section">
    <h2 id="detail-title">Run Detail</h2>
    <table>
      <thead>
        <tr>
          <th>Symbol</th><th>Score</th><th>Direction</th>
          <th>RSI</th><th>VWAP</th><th>EMA</th><th>Vol Ratio</th><th>Momentum</th><th>RS vs SPY</th>
        </tr>
      </thead>
      <tbody id="symbols-body"></tbody>
    </table>
  </div>
  <div class="section" id="contracts-section">
    <h2>Contracts</h2>
    <div id="contracts-body"></div>
  </div>
</div>

</main>

<script>
async function loadRuns() {
  document.getElementById('status').textContent = 'Loading...';
  const res = await fetch('/runs?limit=30');
  const runs = await res.json();
  const body = document.getElementById('runs-body');
  body.innerHTML = '';
  for (const r of runs) {
    const dur = r.completed_at
      ? Math.round((new Date(r.completed_at) - new Date(r.started_at)) / 1000) + 's'
      : '—';
    const tagClass = r.status === 'completed' ? 'tag-completed' : r.status === 'running' ? 'tag-running' : 'tag-failed';
    body.innerHTML += `<tr class="run-row" onclick="loadRun(${r.id})">
      <td>#${r.id}</td>
      <td>${new Date(r.started_at).toLocaleString()}</td>
      <td>${dur}</td>
      <td><span class="tag ${tagClass}">${r.status}</span></td>
      <td>${r.phase}</td>
      <td>${r.symbols_scanned || '—'}</td>
      <td>${r.contracts_found || '—'}</td>
    </tr>`;
  }
  document.getElementById('status').textContent = `${runs.length} runs loaded`;
  pollStatus();
}

async function pollStatus() {
  const res = await fetch('/status');
  const s = await res.json();
  if (s.running) {
    document.getElementById('status').textContent = `&#9889; Running scan...`;
    setTimeout(pollStatus, 3000);
  }
}

async function triggerRun() {
  document.getElementById('status').textContent = '&#9889; Starting scan...';
  await fetch('/run', { method: 'POST', headers: {'Content-Type':'application/json'}, body: '{}' });
  setTimeout(() => { loadRuns(); pollStatus(); }, 1000);
}

async function loadRun(runId) {
  const res = await fetch(`/runs/${runId}`);
  const data = await res.json();

  document.getElementById('detail-title').textContent = `Run #${runId}`;
  document.getElementById('list-view').style.display = 'none';
  document.getElementById('detail-view').style.display = 'block';

  // Symbol scans table
  const sb = document.getElementById('symbols-body');
  sb.innerHTML = '';
  for (const s of data.symbol_scans) {
    const dirClass = s.direction === 'BULLISH' ? 'bullish' : s.direction === 'BEARISH' ? 'bearish' : 'neutral';
    const scoreClass = s.technical_score >= 65 ? 'score-high' : s.technical_score >= 45 ? 'score-mid' : 'score-low';
    const ema = s.ema_aligned ? '✓✓✓' : (s.ema9 > s.ema21 ? '✓✓' : '✗');
    const vwap = s.vwap_position === 'ABOVE' ? '▲' : '▼';
    sb.innerHTML += `<tr>
      <td><b>${s.symbol}</b></td>
      <td class="${scoreClass}">${s.technical_score}</td>
      <td class="${dirClass}">${s.direction}</td>
      <td>${s.rsi || '—'}</td>
      <td>${vwap}</td>
      <td>${ema}</td>
      <td>${s.volume_ratio || '—'}x</td>
      <td>${s.momentum_pct !== null ? s.momentum_pct + '%' : '—'}</td>
      <td>${s.relative_strength !== null ? s.relative_strength + '%' : '—'}</td>
    </tr>`;
  }

  // Contracts grouped by symbol
  const cb = document.getElementById('contracts-body');
  cb.innerHTML = '';
  const bySymbol = {};
  for (const c of data.contracts) {
    if (!bySymbol[c.symbol]) bySymbol[c.symbol] = [];
    bySymbol[c.symbol].push(c);
  }
  for (const [sym, contracts] of Object.entries(bySymbol)) {
    let rows = `<table><thead><tr>
      <th>Contract</th><th>DTE</th><th>Strike</th><th>Delta</th>
      <th>IV</th><th>OI</th><th>Volume</th><th>Spread</th><th>Score</th>
    </tr></thead><tbody>`;
    for (const c of contracts) {
      const scoreClass = c.contract_score >= 70 ? 'score-high' : c.contract_score >= 50 ? 'score-mid' : 'score-low';
      rows += `<tr>
        <td>${c.contract_symbol}</td>
        <td>${c.dte}</td>
        <td>$${c.strike}</td>
        <td>${c.delta}</td>
        <td>${(c.iv * 100).toFixed(1)}%</td>
        <td>${c.open_interest?.toLocaleString() || '—'}</td>
        <td>${c.day_volume?.toLocaleString() || '—'}</td>
        <td>${c.spread_pct}%</td>
        <td class="${scoreClass}">${c.contract_score}</td>
      </tr>`;
    }
    rows += '</tbody></table>';
    cb.innerHTML += `<div class="contract-group"><h3>${sym}</h3>${rows}</div>`;
  }
}

function showList() {
  document.getElementById('list-view').style.display = 'block';
  document.getElementById('detail-view').style.display = 'none';
}

loadRuns();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML
