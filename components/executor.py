"""
components/executor.py — EXECUTOR: Simulated paper execution (Phase 5)

Reads GUARDIAN APPROVED trades and records them as open positions in the DB.

In sim mode (default and recommended):
  - Fetches live entry price from yfinance (bid/ask mid + 0.5% slippage)
  - Records trade as status='open' so the monitor tracks it immediately
  - No Tastytrade orders placed

In dry_run mode:
  - Records as status='pending' (not tracked by monitor — for review only)

In live mode (requires explicit TT_PAPER=false + --live flag):
  - Places real orders via Tastytrade — CHECK WITH USER FIRST

Standalone:
  python -m components.executor --run-id 42         # sim mode (default)
  python -m components.executor --run-id 42 --dry   # dry run (pending)
"""

import argparse
import json
import re
import time
from datetime import date, datetime

import yfinance as yf

from shared import db
from shared.config import RISK


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [executor] {msg}")


def _parse_json(row: dict) -> dict:
    if not row:
        return {}
    try:
        d = row["output_data"]
        return json.loads(d) if isinstance(d, str) else d
    except Exception:
        return {}


def _parse_occ(contract_symbol: str) -> dict | None:
    """Parse OCC symbol e.g. 'AVGO261003C02000000'."""
    m = re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})$', contract_symbol.strip())
    if not m:
        return None
    sym, yymmdd, opt, strike_raw = m.groups()
    try:
        exp = date(int("20" + yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
    except ValueError:
        return None
    return {
        "symbol":      sym,
        "expiry":      str(exp),
        "option_type": "call" if opt == "C" else "put",
        "strike":      int(strike_raw) / 1000.0,
    }


def _fetch_live_mid(contract_symbol: str, fallback_price: float) -> float:
    """
    Fetch current option mid price from yfinance.
    Applies sim_entry_slippage_pct to simulate realistic fill.
    Falls back to fallback_price if yfinance unavailable.
    """
    info = _parse_occ(contract_symbol)
    if not info:
        return fallback_price
    try:
        ticker = yf.Ticker(info["symbol"])
        chain = ticker.option_chain(info["expiry"])
        df = chain.calls if info["option_type"] == "call" else chain.puts
        row = df[df["contractSymbol"] == contract_symbol]
        if row.empty:
            row = df[abs(df["strike"] - info["strike"]) < 0.01]
        if row.empty:
            return fallback_price
        r = row.iloc[0]
        bid = float(r.get("bid", 0) or 0)
        ask = float(r.get("ask", 0) or 0)
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2
            # Sim entry: pay slightly above mid to simulate getting filled at ask side
            slippage = 1 + RISK["sim_entry_slippage_pct"] / 100
            return round(mid * slippage, 2)
        last = float(r.get("lastPrice", 0) or 0)
        return last if last > 0 else fallback_price
    except Exception as e:
        _log(f"yfinance price fetch failed for {contract_symbol}: {e}")
        return fallback_price


def _infer_details(run_id: int, contract_symbol: str) -> dict:
    """Pull option_type, strike, expiry from contract_scans or HUNTER output."""
    contracts = db.get_contract_scans(run_id)
    for c in contracts:
        if c["contract_symbol"] == contract_symbol:
            return {
                "contract_scan_id": c["id"],
                "option_type":      c["option_type"].upper(),
                "strike":           float(c["strike"]),
                "expiry":           str(c["expiry"]),
                "dte_at_entry":     c["dte"],
                "symbol":           c["symbol"],
            }
    hunter_row = db.get_agent_output(run_id, "hunter")
    if hunter_row:
        hunter = _parse_json(hunter_row)
        for p in hunter.get("picks", []):
            if p.get("contract_symbol") == contract_symbol:
                return {
                    "contract_scan_id": None,
                    "option_type":      p.get("option_type", "CALL").upper(),
                    "strike":           float(p.get("strike", 0)),
                    "expiry":           p.get("expiry", ""),
                    "dte_at_entry":     p.get("dte"),
                    "symbol":           p.get("symbol", ""),
                }
    return {"contract_scan_id": None, "option_type": "CALL", "strike": 0,
            "expiry": "", "dte_at_entry": None, "symbol": ""}


def run(run_id: int, dry_run: bool = False, sim: bool = True) -> list[dict]:
    """
    Execute GUARDIAN APPROVED trades.

    sim=True (default): fetch live entry price → status='open' → monitor tracks it
    dry_run=True:       record as status='pending' without going open (human review)
    sim=False, dry_run=False: place real Tastytrade orders (requires explicit intent)
    """
    guardian_row = db.get_agent_output(run_id, "guardian")
    guardian     = _parse_json(guardian_row) if guardian_row else {}
    approved     = [d for d in guardian.get("final_decisions", []) if d.get("verdict") == "APPROVED"]

    if not approved:
        _log("No GUARDIAN APPROVED decisions — nothing to execute")
        return []

    mode = "SIM" if sim else ("DRY RUN" if dry_run else "LIVE")
    _log(f"Executing {len(approved)} approved trade(s) | mode={mode}")

    created = []
    for decision in approved:
        contract_symbol = decision.get("contract_symbol", "")
        details = _infer_details(run_id, contract_symbol)

        # Idempotency: skip if already open for this run
        open_trades = db.get_open_trades()
        if any(t["run_id"] == run_id and t["contract_symbol"] == contract_symbol for t in open_trades):
            _log(f"  SKIP {contract_symbol} — already open for run {run_id}")
            continue

        # Use live mid + slippage as entry price in sim; fall back to GUARDIAN price
        guardian_entry = decision.get("entry_price", 0)
        if sim:
            entry_price = _fetch_live_mid(contract_symbol, fallback_price=guardian_entry)
            _log(f"  {contract_symbol}: live entry=${entry_price:.2f} "
                 f"(guardian=${guardian_entry:.2f})")
        else:
            entry_price = guardian_entry

        stop_price   = decision.get("stop_loss", 0)
        target_price = decision.get("profit_target_1", 0)
        contracts    = decision.get("contracts", 1)

        trade_data = {
            "run_id":           run_id,
            "contract_scan_id": details["contract_scan_id"],
            "symbol":           details["symbol"],
            "contract_symbol":  contract_symbol,
            "option_type":      details["option_type"],
            "direction":        "BULLISH" if details["option_type"] == "CALL" else "BEARISH",
            "strike":           details["strike"],
            "expiry":           details["expiry"] or None,
            "dte_at_entry":     details["dte_at_entry"],
            "decision":         "TRADE",
            "sentinel_flagged": False,
            "guardian_blocked": False,
            "entry_price":      entry_price,
            "stop_price":       stop_price,
            "target_price":     target_price,
            "contracts":        contracts,
            "status":           "pending",
            "notes": {
                "dollar_risk":     decision.get("dollar_risk"),
                "risk_pct":        decision.get("risk_pct"),
                "target_2":        decision.get("profit_target_2"),
                "mode":            mode,
                "guardian_entry":  guardian_entry,
            },
        }

        trade_id = db.insert_trade(run_id, trade_data)

        if sim:
            # Open immediately in DB — monitor will start tracking this right away
            db.open_trade(trade_id, order_id=None)
            _log(f"  SIM OPEN {contract_symbol} x{contracts} "
                 f"entry=${entry_price:.2f}  stop=${stop_price:.2f}  "
                 f"target=${target_price:.2f}")
            status = "open"
        elif not dry_run:
            from shared.tastytrade_client import place_order
            order_id = place_order(contract_symbol, "buy_to_open", contracts)
            if order_id:
                db.open_trade(trade_id, order_id)
                _log(f"  LIVE {contract_symbol} x{contracts} → order_id={order_id}")
            else:
                _log(f"  FAILED {contract_symbol} — place_order returned None")
            status = "open" if order_id else "pending"
        else:
            _log(f"  PENDING {contract_symbol} x{contracts} entry=${entry_price:.2f}  [dry_run]")
            status = "pending"

        created.append({
            "trade_id":        trade_id,
            "contract_symbol": contract_symbol,
            "contracts":       contracts,
            "entry_price":     entry_price,
            "status":          status,
        })

    _log(f"Executor done — {len(created)} trade(s) | mode={mode}")
    return created


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEXUS EXECUTOR")
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--dry",    action="store_true", help="Record as pending (no open)")
    parser.add_argument("--live",   action="store_true", help="Place real Tastytrade orders")
    args = parser.parse_args()
    sim     = not args.dry and not args.live
    dry_run = args.dry
    trades = run(args.run_id, dry_run=dry_run, sim=sim)
    print(f"\nEXECUTOR: {len(trades)} trade(s)\n{json.dumps(trades, indent=2)}")
