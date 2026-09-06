"""
components/options_scanner.py — Options chain scanner

Reads top-ranked symbol_scans for a run_id (BULLISH or BEARISH only),
fetches options chains via Tastytrade DXFeed, applies filters, scores
contracts, and writes to contract_scans.

Standalone usage:
  python -m components.options_scanner --run-id 42
  python -m components.options_scanner --run-id 42 --symbol NVDA
"""

import argparse
from datetime import datetime, date

from shared import db
from shared.config import SCANNER, QUANT
from shared.tastytrade_client import (
    get_option_expirations,
    get_option_chain_structure,
    get_dxfeed_data,
)


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [scanner] {msg}")


def _dte(expiry_str: str) -> int:
    try:
        return (date.fromisoformat(expiry_str) - date.today()).days
    except Exception:
        return -1


def _score_contract(delta_abs: float, spread_pct: float, oi: int, volume: int, dte: int) -> float:
    """Score 0-100. Higher = better contract quality."""
    s = 0.0

    # Delta sweet spot 0.40-0.60 (30 pts)
    if 0.40 <= delta_abs <= 0.60:
        s += 30
    elif 0.35 <= delta_abs < 0.40 or 0.60 < delta_abs <= 0.65:
        s += 20
    else:
        s += 10

    # Spread tightness (25 pts)
    if spread_pct < 1.0:
        s += 25
    elif spread_pct < 2.0:
        s += 20
    elif spread_pct < 3.0:
        s += 12
    else:
        s += 5

    # Open interest (20 pts)
    if oi >= 10_000:
        s += 20
    elif oi >= 5_000:
        s += 16
    elif oi >= 2_000:
        s += 12
    elif oi >= 500:
        s += 8
    else:
        s += 3

    # Day volume (15 pts)
    if volume >= 5_000:
        s += 15
    elif volume >= 1_000:
        s += 11
    elif volume >= 500:
        s += 7
    else:
        s += 3

    # DTE sweet spot 14-30 (10 pts)
    if 14 <= dte <= 30:
        s += 10
    elif 10 <= dte < 14 or 30 < dte <= 40:
        s += 6
    else:
        s += 3

    return round(s, 1)


def scan_symbol(symbol: str, direction: str, symbol_scan_id: int, run_id: int, price: float):
    """
    Scan all qualifying expiries for a symbol and write top contracts to DB.
    Returns number of contracts written.
    """
    option_type = "call" if direction == "BULLISH" else "put"
    expirations = get_option_expirations(symbol)
    if not expirations:
        _log(f"{symbol}: no expirations available")
        return 0

    today = date.today()
    qualifying_expiries = [
        e for e in expirations
        if SCANNER["min_dte"] <= _dte(e) <= SCANNER["max_dte"]
    ]
    if not qualifying_expiries:
        _log(f"{symbol}: no expiries in DTE range {SCANNER['min_dte']}-{SCANNER['max_dte']}")
        return 0

    _log(f"{symbol} ({direction}): scanning {len(qualifying_expiries)} expiries")

    all_contracts = []

    for expiry in qualifying_expiries:
        dte = _dte(expiry)
        strikes = get_option_chain_structure(symbol, expiry)
        if not strikes:
            continue

        # Collect the relevant streamer symbols (call or put side)
        streamer_map = {}  # streamer_symbol -> strike info
        for s in strikes:
            ss = s["call_streamer"] if option_type == "call" else s["put_streamer"]
            occ = s["call_occ"] if option_type == "call" else s["put_occ"]
            if ss:
                streamer_map[ss] = {
                    "strike":      s["strike"],
                    "option_type": option_type,
                    "expiry":      expiry,
                    "dte":         dte,
                    "occ":         occ,
                }

        if not streamer_map:
            continue

        # Fetch live data for all streamer symbols in one DXFeed call
        dxfeed = get_dxfeed_data(list(streamer_map.keys()), timeout=SCANNER["dxfeed_timeout"])

        for ss, info in streamer_map.items():
            data = dxfeed.get(ss, {})
            if not data:
                continue

            bid = float(data.get("bid") or 0)
            ask = float(data.get("ask") or 0)
            if bid <= 0 or ask <= 0:
                continue

            mid = (bid + ask) / 2
            spread_pct = ((ask - bid) / mid * 100) if mid > 0 else 999

            delta_raw = float(data.get("delta") or 0)
            delta_abs = abs(delta_raw)
            iv        = float(data.get("iv") or 0)
            oi        = int(data.get("open_interest") or 0)
            volume    = int(data.get("day_volume") or 0)

            # ── Filters ────────────────────────────────────────────────
            if not (SCANNER["min_delta"] <= delta_abs <= SCANNER["max_delta"]):
                continue
            if spread_pct > SCANNER["max_spread_pct"]:
                continue
            if oi < SCANNER["min_oi"]:
                continue
            if volume < SCANNER["min_volume"]:
                continue
            if iv > SCANNER["max_iv"]:
                continue

            contract_score = _score_contract(delta_abs, spread_pct, oi, volume, dte)

            all_contracts.append({
                "symbol":          symbol,
                "contract_symbol": info["occ"],
                "expiry":          expiry,
                "dte":             dte,
                "strike":          info["strike"],
                "option_type":     option_type,
                "bid":             round(bid, 4),
                "ask":             round(ask, 4),
                "mid":             round(mid, 4),
                "spread_pct":      round(spread_pct, 2),
                "open_interest":   oi,
                "day_volume":      volume,
                "delta":           round(delta_raw, 4),
                "gamma":           round(float(data.get("gamma") or 0), 6),
                "theta":           round(float(data.get("theta") or 0), 4),
                "vega":            round(float(data.get("vega")  or 0), 4),
                "iv":              round(iv, 4),
                "contract_score":  contract_score,
            })

    # Keep top N by score
    all_contracts.sort(key=lambda c: c["contract_score"], reverse=True)
    top = all_contracts[:SCANNER["top_contracts"]]

    for contract in top:
        db.insert_contract_scan(run_id, symbol_scan_id, contract)
        _log(f"  {contract['contract_symbol']:<30} "
             f"DTE={contract['dte']:>3}  "
             f"Δ={contract['delta']:+.2f}  "
             f"IV={contract['iv']*100:.1f}%  "
             f"OI={contract['open_interest']:>6,}  "
             f"Vol={contract['day_volume']:>6,}  "
             f"Spread={contract['spread_pct']:.1f}%  "
             f"Score={contract['contract_score']:.1f}")

    return len(top)


def run(run_id: int, symbol_filter: str = None):
    """
    Process top-ranked BULLISH/BEARISH symbols from symbol_scans.
    Writes contracts to contract_scans. Returns total contracts written.
    """
    scans = db.get_symbol_scans(run_id)
    if not scans:
        _log(f"No symbol scans found for run_id={run_id}")
        return 0

    # Only scan symbols with a directional signal; cap at top_symbols_to_scan
    directional = [
        s for s in scans
        if s["direction"] in ("BULLISH", "BEARISH")
    ]
    if symbol_filter:
        directional = [s for s in directional if s["symbol"] == symbol_filter.upper()]
    else:
        directional = directional[:QUANT["top_symbols_to_scan"]]

    _log(f"{len(directional)} directional symbols to options-scan | run_id={run_id}")

    total = 0
    for scan in directional:
        symbol    = scan["symbol"]
        direction = scan["direction"]
        price     = float(scan["price"] or 0)
        _log(f"→ {symbol} ({direction}, score={scan['technical_score']})")
        n = scan_symbol(symbol, direction, scan["id"], run_id, price)
        total += n

    _log(f"Options scan complete — {total} contracts written")
    return total


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEXUS options scanner")
    parser.add_argument("--run-id", type=int, required=True, help="Run ID to process")
    parser.add_argument("--symbol", type=str, help="Scan a single symbol only")
    args = parser.parse_args()

    run(args.run_id, symbol_filter=args.symbol)
