"""
components/executor.py — EXECUTOR: Paper trade execution (Phase 5)

Reads GUARDIAN APPROVED trades, records them to the trades table,
and optionally places orders via Tastytrade (dry_run=False).

By default (dry_run=True) it records to DB with status='pending'
without placing any real orders. POST /execute/{run_id} in the API
triggers with dry_run=False after human review.

DB in:  agent_outputs (guardian, hunter), contract_scans
DB out: trades table

Standalone:
  python -m components.executor --run-id 42                # dry run
  python -m components.executor --run-id 42 --live         # real orders
"""

import argparse
import json
from datetime import datetime, date

from shared import db
from shared.tastytrade_client import place_order, is_mock_mode, TT_PAPER


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


def _find_contract_scan_id(run_id: int, contract_symbol: str) -> int | None:
    """Look up the contract_scan row for this contract."""
    contracts = db.get_contract_scans(run_id)
    for c in contracts:
        if c["contract_symbol"] == contract_symbol:
            return c["id"]
    return None


def _infer_details(run_id: int, contract_symbol: str, guardian_decision: dict) -> dict:
    """Pull option_type, strike, expiry from contract_scans if available."""
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
    # Fallback: parse from HUNTER picks
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
    return {"contract_scan_id": None, "option_type": "CALL", "strike": 0, "expiry": "", "dte_at_entry": None, "symbol": ""}


def run(run_id: int, dry_run: bool = True) -> list[dict]:
    """
    Execute GUARDIAN APPROVED trades.

    dry_run=True  → record to trades table as 'pending', no orders placed
    dry_run=False → record + place orders via Tastytrade, status='open'

    Returns list of trade records created.
    """
    guardian_row = db.get_agent_output(run_id, "guardian")
    guardian     = _parse_json(guardian_row) if guardian_row else {}
    approved     = [d for d in guardian.get("final_decisions", []) if d.get("verdict") == "APPROVED"]

    if not approved:
        _log("No GUARDIAN APPROVED decisions — nothing to execute")
        return []

    mode = "DRY RUN" if dry_run else ("MOCK" if is_mock_mode() else ("PAPER" if TT_PAPER else "LIVE"))
    _log(f"Executing {len(approved)} approved trade(s) | mode={mode}")

    created = []
    for decision in approved:
        contract_symbol = decision.get("contract_symbol", "")
        details = _infer_details(run_id, contract_symbol, decision)

        # Check idempotency — don't double-insert same contract for same run
        open_trades = db.get_open_trades()
        already_open = any(
            t["run_id"] == run_id and t["contract_symbol"] == contract_symbol
            for t in open_trades
        )
        if already_open:
            _log(f"  SKIP {contract_symbol} — already in open trades for run {run_id}")
            continue

        entry_price  = decision.get("entry_price", 0)
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
                "dollar_risk": decision.get("dollar_risk"),
                "risk_pct":    decision.get("risk_pct"),
                "target_2":    decision.get("profit_target_2"),
                "dry_run":     dry_run,
                "mode":        mode,
            },
        }

        trade_id = db.insert_trade(run_id, trade_data)

        if not dry_run:
            order_id = place_order(contract_symbol, "buy_to_open", contracts)
            if order_id:
                db.open_trade(trade_id, order_id)
                _log(f"  PLACED {contract_symbol} x{contracts} → order_id={order_id}")
            else:
                _log(f"  FAILED {contract_symbol} — place_order returned None")
        else:
            _log(f"  PENDING {contract_symbol} x{contracts} "
                 f"entry=${entry_price:.2f}  stop=${stop_price:.2f}  "
                 f"target=${target_price:.2f}  [dry_run]")

        created.append({
            "trade_id":        trade_id,
            "contract_symbol": contract_symbol,
            "contracts":       contracts,
            "entry_price":     entry_price,
            "status":          "open" if not dry_run else "pending",
        })

    _log(f"Executor done — {len(created)} trade(s) recorded | dry_run={dry_run}")
    return created


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEXUS EXECUTOR paper trade execution")
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--live",   action="store_true", help="Place real orders (default: dry run)")
    args = parser.parse_args()
    trades = run(args.run_id, dry_run=not args.live)
    print(f"\nEXECUTOR: {len(trades)} trade(s)\n{json.dumps(trades, indent=2)}")
