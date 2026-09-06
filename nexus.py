"""
nexus.py — NEXUS Phase 1 Orchestrator

Runs the full Phase 1 pipeline:
  1. FLOW SCANNER  → symbol_universe
  2. QUANT ENGINE  → symbol_scans
  3. OPTIONS SCANNER → contract_scans
  4. Print report

Usage:
  python nexus.py
  python nexus.py --symbols NVDA,AAPL,TSLA
  python nexus.py --resume-run-id 42        # skip flow+quant, re-run scanner only
  python nexus.py --init-db                 # create tables and exit
"""

import argparse
import sys
from datetime import datetime

from shared import db
from shared.config import SCAN_UNIVERSE
import components.flow_scanner  as flow_scanner
import components.quant         as quant
import components.options_scanner as options_scanner


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [nexus] {msg}")


def _separator(char: str = "─", width: int = 70):
    print(char * width)


def _print_report(run_id: int):
    _separator("═")
    print(f"  NEXUS — PHASE 1 REPORT   run_id={run_id}   {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    _separator("═")

    scans = db.get_symbol_scans(run_id)
    if not scans:
        print("  No symbol scans found.")
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
            # Find the parent symbol scan score
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


def run_pipeline(symbols: list[str] = None, resume_run_id: int = None):
    symbols = symbols or SCAN_UNIVERSE

    if resume_run_id:
        run_id = resume_run_id
        _log(f"Resuming run_id={run_id} — skipping flow+quant, re-running options scanner")
        n = options_scanner.run(run_id)
        _print_report(run_id)
        db.complete_run(run_id,
                        symbols_scanned=len(db.get_symbol_scans(run_id)),
                        contracts_found=n)
        return run_id

    run_id = db.create_run(phase=1)
    _log(f"Starting Phase 1 pipeline | run_id={run_id} | {len(symbols)} symbols")

    try:
        # ── Step 1: Flow scanner ──────────────────────────────────────────
        _log("Step 1/3 — Flow scanner")
        flow_scanner.run(run_id, symbols)

        # ── Step 2: Quant engine ──────────────────────────────────────────
        _log("Step 2/3 — Quant engine")
        quant.run(run_id)

        # ── Step 3: Options scanner ───────────────────────────────────────
        _log("Step 3/3 — Options scanner")
        n_contracts = options_scanner.run(run_id)

        n_symbols = len(db.get_symbol_scans(run_id))
        db.complete_run(run_id, symbols_scanned=n_symbols, contracts_found=n_contracts)
        _log(f"Pipeline complete | run_id={run_id}")

    except Exception as e:
        db.fail_run(run_id, notes=str(e))
        _log(f"Pipeline failed: {e}")
        raise

    _print_report(run_id)
    return run_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEXUS Phase 1 orchestrator")
    parser.add_argument("--symbols",       type=str, help="Comma-separated symbol override")
    parser.add_argument("--resume-run-id", type=int, help="Resume from an existing run (options scan only)")
    parser.add_argument("--init-db",       action="store_true", help="Initialise DB schema and exit")
    args = parser.parse_args()

    if args.init_db:
        db.init_schema()
        print("DB schema initialised. Exiting.")
        sys.exit(0)

    symbols = args.symbols.split(",") if args.symbols else None
    run_pipeline(symbols=symbols, resume_run_id=args.resume_run_id)
