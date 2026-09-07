"""
nexus.py — NEXUS Phase 3 Orchestrator

Full pipeline:
  Phase 2:  SCOUT → ATLAS → COMPASS
  Phase 1:  flow_scanner → quant → options_scanner
  Phase 3:  HUNTER → EDGE → ANALYST → report

Usage:
  python nexus.py                            # full Phase 3 pipeline
  python nexus.py --phase1                   # flow+quant+scanner only (no LLM)
  python nexus.py --phase2                   # Phase 2 + scan, skip HUNTER/EDGE/ANALYST
  python nexus.py --symbols NVDA,AAPL,TSLA  # symbol override (phase1 mode)
  python nexus.py --resume-run-id 42         # re-run from options scanner only
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
import components.hunter          as hunter
import components.edge            as edge
import components.analyst         as analyst


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [nexus] {msg}")


def _separator(char: str = "─", width: int = 70):
    print(char * width)


def _pj(row: dict) -> dict:
    """Parse agent_output row's output_data field."""
    if not row:
        return {}
    try:
        d = row["output_data"]
        return json.loads(d) if isinstance(d, str) else d
    except Exception:
        return {}


def _print_analyst_brief(run_id: int):
    row = db.get_agent_output(run_id, "analyst")
    if not row:
        return
    d = _pj(row)
    headline = d.get("headline", "")
    brief    = d.get("brief") or d.get("full_response", "")
    actions  = d.get("action_items", [])
    risks    = d.get("risk_factors", [])
    conf     = d.get("confidence", "?")

    _separator("═")
    print(f"  ANALYST BRIEF  (confidence={conf}%)")
    _separator("═")
    if headline:
        print(f"\n  {headline}\n")
    if brief:
        for line in brief.splitlines():
            print(f"  {line}")
    if actions:
        print("\n  ACTION ITEMS:")
        for i, a in enumerate(actions, 1):
            print(f"    {i}. {a}")
    if risks:
        print("\n  RISK FACTORS:")
        for r in risks:
            print(f"    • {r}")


def _print_edge_decisions(run_id: int):
    row = db.get_agent_output(run_id, "edge")
    if not row:
        return
    d = _pj(row)
    decisions = d.get("decisions", [])
    if not decisions:
        return

    print(f"\n  EDGE DECISIONS  (session={d.get('session','?')}, "
          f"overall_go={d.get('overall_go','?')})")
    _separator()
    for dec in decisions:
        v = dec.get("verdict", "?")
        sym = dec.get("contract_symbol", "?")
        if v == "GO":
            print(f"  GO     {sym:<36} "
                  f"limit=${dec.get('limit_price',0):.2f}  "
                  f"stop=${dec.get('stop_loss',0):.2f}  "
                  f"target=${dec.get('profit_target_1',0):.2f}/${dec.get('profit_target_2',0):.2f}  "
                  f"qty={dec.get('suggested_contracts',1)}")
            if dec.get("timing_note"):
                print(f"         {dec['timing_note']}")
        else:
            reason = dec.get("no_go_reason") or dec.get("timing_note", "")
            print(f"  {v:<6} {sym:<36} {reason}")


def _print_agent_summary(run_id: int):
    """Print ATLAS + COMPASS context (Phase 2) and HUNTER picks."""
    atlas_row   = db.get_agent_output(run_id, "atlas")
    compass_row = db.get_agent_output(run_id, "compass")
    hunter_row  = db.get_agent_output(run_id, "hunter")

    if atlas_row:
        d = _pj(atlas_row)
        print(f"\n  ATLAS: {d.get('regime','?')} "
              f"(confidence={d.get('confidence','?')}%, "
              f"risk={d.get('risk_level','?')}, "
              f"VIX={d.get('vix_signal','?')}, "
              f"breadth={d.get('breadth','?')})")
        print(f"  Favored: {d.get('favored_sectors',[])}  |  "
              f"Avoid: {d.get('avoid_sectors',[])}")

    if compass_row:
        d = _pj(compass_row)
        top = [s["etf"] for s in d.get("ranked_sectors", [])[:5]]
        filtered = d.get("filtered_universe", [])
        print(f"\n  COMPASS: bias={d.get('direction_bias','?')}  "
              f"sectors={top}")
        print(f"  Universe ({len(filtered)}): {', '.join(filtered)}")
        if d.get("avoid_symbols"):
            print(f"  Avoiding: {d['avoid_symbols']}")

    if hunter_row:
        d = _pj(hunter_row)
        picks = d.get("picks", [])
        print(f"\n  HUNTER: {len(picks)} pick(s)")
        for p in picks:
            print(f"    {p.get('contract_symbol','?'):<36}  "
                  f"entry=${p.get('entry_price',0):.2f}  "
                  f"stop=${p.get('stop_loss',0):.2f}  "
                  f"conviction={p.get('conviction','?')}")
            thesis = p.get("thesis", "")
            if thesis:
                print(f"    Thesis: {thesis[:120]}")

    _print_edge_decisions(run_id)


def _print_report(run_id: int):
    _separator("═")
    print(f"  NEXUS — PHASE 3 REPORT   run_id={run_id}   {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    _separator("═")

    _print_agent_summary(run_id)

    scans = db.get_symbol_scans(run_id)
    if scans:
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
    if contracts:
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

    # ANALYST brief always printed last
    _print_analyst_brief(run_id)


def run_pipeline(
    symbols: list[str] = None,
    resume_run_id: int = None,
    phase1_only: bool = False,
    phase2_only: bool = False,
):
    if resume_run_id:
        run_id = resume_run_id
        _log(f"Resuming run_id={run_id} — re-running from options scanner")
        n = options_scanner.run(run_id)
        _print_report(run_id)
        db.complete_run(run_id,
                        symbols_scanned=len(db.get_symbol_scans(run_id)),
                        contracts_found=n)
        return run_id

    phase = 1 if phase1_only else (2 if phase2_only else 3)
    run_id = db.create_run(phase=phase)
    _log(f"Starting Phase {phase} pipeline | run_id={run_id}")

    try:
        if not phase1_only:
            # ── Phase 2: Intelligence layer ───────────────────────────────────
            _log("Step 1/9 — SCOUT (research)")
            scout.run(run_id)

            _log("Step 2/9 — ATLAS (regime)")
            atlas.run(run_id)

            _log("Step 3/9 — COMPASS (sector rotation)")
            compass.run(run_id)

            active_symbols = compass.get_filtered_universe(run_id)
            _log(f"Active universe ({len(active_symbols)}): {active_symbols}")
        else:
            active_symbols = symbols or SCAN_UNIVERSE
            _log(f"Phase 1 mode — {len(active_symbols)} symbols, no AI agents")

        # ── Phase 1: Market scan ──────────────────────────────────────────
        step = 4 if not phase1_only else 1
        _log(f"Step {step}/9 — Flow scanner")
        flow_scanner.run(run_id, active_symbols)

        _log(f"Step {step+1}/9 — Quant engine")
        quant.run(run_id)

        _log(f"Step {step+2}/9 — Options scanner")
        n_contracts = options_scanner.run(run_id)

        if not phase1_only and not phase2_only:
            # ── Phase 3: Decision layer ───────────────────────────────────────
            _log("Step 7/9 — HUNTER (contract selection)")
            hunter.run(run_id)

            _log("Step 8/9 — EDGE (entry timing)")
            edge.run(run_id)

            _log("Step 9/9 — ANALYST (trade brief)")
            analyst.run(run_id)

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
    parser = argparse.ArgumentParser(description="NEXUS Phase 3 orchestrator")
    parser.add_argument("--symbols",       type=str, help="Comma-separated symbol override (phase1 mode only)")
    parser.add_argument("--phase1",        action="store_true", help="Flow+quant+scanner only, no AI agents")
    parser.add_argument("--phase2",        action="store_true", help="Phase 2 + scan, skip HUNTER/EDGE/ANALYST")
    parser.add_argument("--resume-run-id", type=int, help="Resume from options scanner on existing run")
    parser.add_argument("--init-db",       action="store_true", help="Initialise DB schema and exit")
    args = parser.parse_args()

    if args.init_db:
        db.init_schema()
        print("DB schema initialised. Exiting.")
        sys.exit(0)

    symbols = args.symbols.split(",") if args.symbols else None
    run_pipeline(
        symbols=symbols,
        resume_run_id=args.resume_run_id,
        phase1_only=args.phase1,
        phase2_only=args.phase2,
    )
