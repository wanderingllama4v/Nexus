"""
nexus.py — NEXUS Phase 2 Orchestrator

Full Phase 2 pipeline:
  1. SCOUT         → agent_outputs (news, macro, earnings, events)
  2. ATLAS         → market regime + runs.market_regime
  3. COMPASS       → sector rotation + filtered symbol universe
  4. FLOW SCANNER  → symbol_universe (using COMPASS-filtered universe)
  5. QUANT ENGINE  → symbol_scans
  6. OPTIONS SCANNER → contract_scans
  7. Print report

Usage:
  python nexus.py
  python nexus.py --symbols NVDA,AAPL,TSLA
  python nexus.py --phase1                   # skip Phase 2 agents (faster)
  python nexus.py --resume-run-id 42         # skip all to options scan only
  python nexus.py --init-db                  # create tables and exit
"""

import argparse
import json
import sys
from datetime import datetime

from shared import db
from shared.config import SCAN_UNIVERSE
import components.flow_scanner    as flow_scanner
import components.quant           as quant
import components.options_scanner as options_scanner
import components.scout           as scout
import components.atlas           as atlas
import components.compass         as compass


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [nexus] {msg}")


def _separator(char: str = "─", width: int = 70):
    print(char * width)


def _print_agent_summary(run_id: int):
    """Print ATLAS regime and COMPASS sector rotation summary."""
    atlas_row = db.get_agent_output(run_id, "atlas")
    if atlas_row:
        try:
            d = json.loads(atlas_row["output_data"]) if isinstance(atlas_row["output_data"], str) else atlas_row["output_data"]
            print(f"\n  ATLAS REGIME: {d.get('regime','?')} "
                  f"(confidence={d.get('confidence','?')}%, "
                  f"risk={d.get('risk_level','?')}, "
                  f"VIX={d.get('vix_signal','?')}, "
                  f"breadth={d.get('breadth','?')})")
            print(f"  Favored: {d.get('favored_sectors',[])}  |  "
                  f"Avoid: {d.get('avoid_sectors',[])}")
            print(f"  {d.get('rationale','')}")
        except Exception:
            pass

    compass_row = db.get_agent_output(run_id, "compass")
    if compass_row:
        try:
            d = json.loads(compass_row["output_data"]) if isinstance(compass_row["output_data"], str) else compass_row["output_data"]
            top = [s["etf"] for s in d.get("ranked_sectors", [])[:5]]
            filtered = d.get("filtered_universe", [])
            avoid_sym = d.get("avoid_symbols", [])
            print(f"\n  COMPASS SECTORS: {top}")
            print(f"  Active universe ({len(filtered)}): {', '.join(filtered)}")
            if avoid_sym:
                reasons = d.get("avoid_reason", {})
                avoid_str = ", ".join(f"{s}({reasons.get(s, '?')})" for s in avoid_sym)
                print(f"  Avoiding: {avoid_str}")
            print(f"  Bias: {d.get('direction_bias','?')} | {d.get('rationale','')}")
        except Exception:
            pass


def _print_report(run_id: int):
    _separator("═")
    print(f"  NEXUS — PHASE 2 REPORT   run_id={run_id}   {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    _separator("═")

    _print_agent_summary(run_id)

    scans = db.get_symbol_scans(run_id)
    if not scans:
        print("\n  No symbol scans found.")
        _separator("═")
        return

    print(f"\n  {'SYMBOL':<8} {'SCORE':>5}  {'DIR':<8}  {'RSI':>5}  "
          f"{'VWAP':>5}  {'EMA':>5}  {'VOL':>5}  {'RS':>7}")
    _separator()

    for s in scans:
        vwap_arrow = "▲" if s["vwap_position"] == "ABOVE" else "▼"
        ema_str    = "✓✓✓" if s["ema_aligned"] else ("✓✓ " if s["ema9"] and s["ema21"] and s["ema9"] > s["ema21"] else "✗  ")
        print(f"  {s['symbol']:<8} {s['technical_score']:>5.1f}  "
              f"{s['direction']:<8}  "
              f"{s['rsi']:>5.1f}  "
              f"  {vwap_arrow}    "
              f"{ema_str}  "
              f"{s['volume_ratio']:>5.1f}x  "
              f"{s['relative_strength']:>+6.2f}%")

    contracts = db.get_contract_scans(run_id)
    if not contracts:
        print("\n  No contracts passed the scanner filters.")
        _separator("═")
        return

    current_sym = None
    for c in contracts:
        if c["symbol"] != current_sym:
            current_sym = c["symbol"]
            sym_scan = next((s for s in scans if s["symbol"] == current_sym), None)
            score_str = f"score={sym_scan['technical_score']:.1f}" if sym_scan else ""
            print(f"\n  CONTRACTS — {current_sym} ({sym_scan['direction'] if sym_scan else ''}, {score_str})")
            print(f"  {'CONTRACT':<32} {'DTE':>4}  {'STRIKE':>7}  "
                  f"{'Δ':>5}  {'IV':>5}  {'OI':>7}  {'VOL':>6}  "
                  f"{'SPREAD':>7}  {'SCORE':>6}")
            _separator()

        print(f"  {c['contract_symbol']:<32} "
              f"{c['dte']:>4}  "
              f"${c['strike']:>6.2f}  "
              f"{c['delta']:>+5.2f}  "
              f"{c['iv']*100:>4.1f}%  "
              f"{c['open_interest']:>7,}  "
              f"{c['day_volume']:>6,}  "
              f"{c['spread_pct']:>6.1f}%  "
              f"{c['contract_score']:>6.1f}")

    _separator("═")
    print(f"  {len(scans)} symbols scored | {len(contracts)} contracts found")
    _separator("═")


def run_pipeline(symbols: list[str] = None, resume_run_id: int = None, phase1_only: bool = False):
    if resume_run_id:
        run_id = resume_run_id
        _log(f"Resuming run_id={run_id} — skipping all agents, re-running options scanner")
        n = options_scanner.run(run_id)
        _print_report(run_id)
        db.complete_run(run_id,
                        symbols_scanned=len(db.get_symbol_scans(run_id)),
                        contracts_found=n)
        return run_id

    run_id = db.create_run(phase=2 if not phase1_only else 1)
    _log(f"Starting {'Phase 1' if phase1_only else 'Phase 2'} pipeline | run_id={run_id}")

    try:
        if not phase1_only:
            # ── Phase 2: Intelligence layer ───────────────────────────────────
            _log("Step 1/6 — SCOUT (Perplexity research)")
            scout.run(run_id)

            _log("Step 2/6 — ATLAS (market regime)")
            atlas.run(run_id)

            _log("Step 3/6 — COMPASS (sector rotation)")
            compass.run(run_id)

            # COMPASS narrows the symbol universe for the downstream steps
            active_symbols = compass.get_filtered_universe(run_id)
            _log(f"COMPASS filtered universe: {active_symbols}")
        else:
            active_symbols = symbols or SCAN_UNIVERSE
            _log(f"Phase 1 mode — using {len(active_symbols)} symbols (no AI agents)")

        # ── Phase 1: Market scan ──────────────────────────────────────────
        step_base = 4 if not phase1_only else 1
        _log(f"Step {step_base}/6 — Flow scanner ({len(active_symbols)} symbols)")
        flow_scanner.run(run_id, active_symbols)

        _log(f"Step {step_base+1}/6 — Quant engine")
        quant.run(run_id)

        _log(f"Step {step_base+2}/6 — Options scanner")
        n_contracts = options_scanner.run(run_id)

        n_symbols = len(db.get_symbol_scans(run_id))
        db.complete_run(run_id, symbols_scanned=n_symbols, contracts_found=n_contracts)
        _log(f"Pipeline complete | run_id={run_id} | {n_symbols} symbols | {n_contracts} contracts")

    except Exception as e:
        db.fail_run(run_id, notes=str(e))
        _log(f"Pipeline failed: {e}")
        raise

    _print_report(run_id)
    return run_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEXUS Phase 2 orchestrator")
    parser.add_argument("--symbols",       type=str, help="Comma-separated symbol override (phase1 mode only)")
    parser.add_argument("--phase1",        action="store_true", help="Skip Phase 2 AI agents (flow+quant+scanner only)")
    parser.add_argument("--resume-run-id", type=int, help="Resume from an existing run (options scan only)")
    parser.add_argument("--init-db",       action="store_true", help="Initialise DB schema and exit")
    args = parser.parse_args()

    if args.init_db:
        db.init_schema()
        print("DB schema initialised. Exiting.")
        sys.exit(0)

    symbols = args.symbols.split(",") if args.symbols else None
    run_pipeline(symbols=symbols, resume_run_id=args.resume_run_id, phase1_only=args.phase1)
