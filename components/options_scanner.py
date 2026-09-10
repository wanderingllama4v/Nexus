"""
components/options_scanner.py — Options chain scanner

Reads top-ranked symbol_scans for a run_id (BULLISH or BEARISH only),
fetches options chains via yfinance (reliable HTTP, no WebSocket hang),
filters and scores contracts, and writes to contract_scans.

Standalone usage:
  python -m components.options_scanner --run-id 42
  python -m components.options_scanner --run-id 42 --symbol NVDA
"""

import argparse
import math
from datetime import datetime, date

import yfinance as yf

from shared import db
from shared.config import SCANNER, QUANT, RISK_FREE_RATE


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [scanner] {msg}")


def _get_expirations(symbol: str) -> list[str]:
    """Get available option expiration dates from yfinance."""
    try:
        return list(yf.Ticker(symbol).options)
    except Exception:
        return []


def _dte(expiry_str: str) -> int:
    try:
        return (date.fromisoformat(expiry_str) - date.today()).days
    except Exception:
        return -1


def _norm_cdf(x: float) -> float:
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0


def _bs_delta(S: float, K: float, T: float, sigma: float, r: float, option_type: str) -> float:
    """Black-Scholes delta. Returns 0 on bad inputs."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
        return _norm_cdf(d1) if option_type == "call" else _norm_cdf(d1) - 1.0
    except Exception:
        return 0.0


def _yfinance_chain(symbol: str, expiry: str, option_type: str, stock_price: float) -> dict:
    """
    Fetch option chain via yfinance for one symbol/expiry/type.
    Returns {strike: {bid, ask, iv, volume, open_interest, delta, gamma, theta, vega, contract_symbol}}
    """
    try:
        ticker = yf.Ticker(symbol)
        chain = ticker.option_chain(expiry)
        df = chain.calls if option_type == "call" else chain.puts
        T = max(_dte(expiry), 0) / 365.0
        result = {}
        for _, row in df.iterrows():
            K = float(row.get("strike", 0) or 0)
            if K <= 0:
                continue
            def _safe_float(v):
                try:
                    f = float(v or 0)
                    return 0.0 if (f != f) else f  # NaN check
                except Exception:
                    return 0.0

            def _safe_int(v):
                try:
                    f = float(v or 0)
                    return 0 if (f != f) else int(f)
                except Exception:
                    return 0

            bid = _safe_float(row.get("bid"))
            ask = _safe_float(row.get("ask"))
            iv = _safe_float(row.get("impliedVolatility"))
            volume = _safe_int(row.get("volume"))
            oi = _safe_int(row.get("openInterest"))
            occ = str(row.get("contractSymbol", "") or "")
            delta = _bs_delta(stock_price, K, T, iv, RISK_FREE_RATE, option_type)
            result[K] = {
                "bid":             bid,
                "ask":             ask,
                "iv":              iv,
                "day_volume":      volume,
                "open_interest":   oi,
                "delta":           delta,
                "gamma":           0.0,
                "theta":           0.0,
                "vega":            0.0,
                "contract_symbol": occ,
            }
        return result
    except Exception as e:
        _log(f"yfinance chain error {symbol} {expiry}: {e}")
        return {}


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
    expirations = _get_expirations(symbol)
    if not expirations:
        _log(f"{symbol}: no expirations available")
        return 0

    qualifying_expiries = [
        e for e in expirations
        if SCANNER["min_dte"] <= _dte(e) <= SCANNER["max_dte"]
    ]
    if not qualifying_expiries:
        _log(f"{symbol}: no expiries in DTE range {SCANNER['min_dte']}-{SCANNER['max_dte']}")
        return 0

    _log(f"{symbol} ({direction}): scanning {len(qualifying_expiries)} expiries via yfinance")

    all_contracts = []

    for expiry in qualifying_expiries:
        dte = _dte(expiry)
        chain_data = _yfinance_chain(symbol, expiry, option_type, price)
        if not chain_data:
            continue

        for K, data in chain_data.items():
            bid = data["bid"]
            ask = data["ask"]
            if bid <= 0 or ask <= 0:
                continue

            mid = (bid + ask) / 2
            spread_pct = ((ask - bid) / mid * 100) if mid > 0 else 999

            delta_raw = data["delta"]
            delta_abs = abs(delta_raw)
            iv = data["iv"]
            oi = data["open_interest"]
            volume = data["day_volume"]
            occ = data["contract_symbol"]

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
                "contract_symbol": occ,
                "expiry":          expiry,
                "dte":             dte,
                "strike":          K,
                "option_type":     option_type,
                "bid":             round(bid, 4),
                "ask":             round(ask, 4),
                "mid":             round(mid, 4),
                "spread_pct":      round(spread_pct, 2),
                "open_interest":   oi,
                "day_volume":      volume,
                "delta":           round(delta_raw, 4),
                "gamma":           0.0,
                "theta":           0.0,
                "vega":            0.0,
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
