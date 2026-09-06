"""
components/flow_scanner.py — Momentum scan + unusual flow detector

Scans SCAN_UNIVERSE for:
  1. Unusual options flow (high vol/OI ratio, large notional)
  2. Momentum context (price change vs prev close)

Writes each symbol to symbol_universe for the run, flagging those with
detected flow so downstream components can weight them accordingly.

Standalone usage:
  python -m components.flow_scanner --run-id 42
  python -m components.flow_scanner              # creates its own run
"""

import argparse
import time
from datetime import datetime

import yfinance as yf

from shared.config import (
    SCAN_UNIVERSE, INDEX_TICKERS, FLOW,
)
from shared import db


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [flow] {msg}")


def _dte(expiry_str: str) -> int:
    try:
        exp = datetime.strptime(expiry_str, "%Y-%m-%d").date()
        return (exp - datetime.now().date()).days
    except Exception:
        return -1


def _confidence(vol: int, oi: int, notional: float, dte: int) -> str:
    score = 0.0
    vol_oi = vol / max(oi, 1)
    if vol_oi >= 10:
        score += 0.40
    elif vol_oi >= 5:
        score += 0.25
    else:
        score += 0.10
    if notional >= 500_000:
        score += 0.35
    elif notional >= 200_000:
        score += 0.25
    else:
        score += 0.05
    if 5 <= dte <= 21:
        score += 0.25
    elif 2 <= dte <= 30:
        score += 0.15
    else:
        score += 0.05
    if score >= 0.70:
        return "high"
    if score >= 0.45:
        return "medium"
    return "low"


def _scan_ticker(ticker: str, max_contract_price: float) -> dict:
    """
    Scan a single ticker for:
      - Current price + prev close (momentum context)
      - Best unusual flow hit per direction (CALL/PUT)

    Returns a dict ready for insert_universe_symbol.
    """
    result = {
        "symbol":           ticker,
        "price":            None,
        "prev_close":       None,
        "change_pct":       None,
        "flow_detected":    False,
        "flow_direction":   None,
        "flow_strike":      None,
        "flow_expiry":      None,
        "flow_volume":      None,
        "flow_oi":          None,
        "flow_vol_oi_ratio":None,
        "flow_notional":    None,
        "flow_confidence":  None,
    }

    try:
        yf_ticker = yf.Ticker(ticker)
        info = yf_ticker.fast_info
        price = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
        prev  = getattr(info, "previous_close", None)
        if not price or price <= 0:
            _log(f"{ticker}: no price data, skipping")
            return result

        price = float(price)
        prev  = float(prev) if prev else price
        result["price"]      = price
        result["prev_close"] = prev
        result["change_pct"] = round((price - prev) / prev * 100, 4) if prev else 0.0

        exp_dates = yf_ticker.options
        if not exp_dates:
            return result

        best_by_dir: dict = {}  # "CALL" | "PUT" -> best hit dict

        for expiry_str in exp_dates:
            dte = _dte(expiry_str)
            if dte < FLOW["min_dte"] or dte > FLOW["max_dte"]:
                continue
            try:
                chain = yf_ticker.option_chain(expiry_str)
            except Exception:
                continue

            for opt_type, df in [("CALL", chain.calls), ("PUT", chain.puts)]:
                if df is None or df.empty:
                    continue
                for _, row in df.iterrows():
                    try:
                        strike   = float(row.get("strike") or 0)
                        volume   = int(row.get("volume") or 0)
                        oi       = int(row.get("openInterest") or 0)
                        bid      = float(row.get("bid") or 0)
                        ask      = float(row.get("ask") or 0)
                        mid      = round((bid + ask) / 2, 2) if bid and ask else 0

                        if strike <= 0 or mid <= 0:
                            continue
                        if mid * 100 > max_contract_price:
                            continue
                        if volume < FLOW["min_raw_volume"]:
                            continue
                        if abs(strike - price) / price > FLOW["atm_pct"]:
                            continue

                        threshold = (FLOW["vol_oi_ratio_index"]
                                     if ticker in INDEX_TICKERS
                                     else FLOW["vol_oi_ratio_stock"])
                        if oi > 0 and volume / oi < threshold:
                            continue
                        if oi == 0 and volume < FLOW["min_raw_volume"] * 2:
                            continue

                        notional = volume * mid * 100
                        if notional < FLOW["min_premium_dollars"]:
                            continue

                        vol_oi_ratio = volume / max(oi, 1)
                        conf = _confidence(volume, oi, notional, dte)
                        if conf == "low":
                            continue

                        hit = {
                            "direction":    opt_type,
                            "strike":       strike,
                            "expiry":       expiry_str,
                            "volume":       volume,
                            "oi":           oi,
                            "vol_oi_ratio": round(vol_oi_ratio, 2),
                            "notional":     notional,
                            "confidence":   conf,
                        }
                        existing = best_by_dir.get(opt_type)
                        if existing is None or notional > existing["notional"]:
                            best_by_dir[opt_type] = hit

                    except Exception:
                        continue

        if best_by_dir:
            # Prefer CALL if both directions detected; downstream can see both via the DB
            best = best_by_dir.get("CALL") or best_by_dir.get("PUT")
            result["flow_detected"]    = True
            result["flow_direction"]   = best["direction"]
            result["flow_strike"]      = best["strike"]
            result["flow_expiry"]      = best["expiry"]
            result["flow_volume"]      = best["volume"]
            result["flow_oi"]          = best["oi"]
            result["flow_vol_oi_ratio"]= best["vol_oi_ratio"]
            result["flow_notional"]    = best["notional"]
            result["flow_confidence"]  = best["confidence"]
            _log(f"{ticker}: flow {best['direction']} ${best['strike']} "
                 f"exp {best['expiry']} notional=${best['notional']:,.0f} [{best['confidence'].upper()}]")
        else:
            _log(f"{ticker}: no unusual flow | price=${price:.2f} chg={result['change_pct']:+.2f}%")

    except Exception as e:
        _log(f"{ticker}: error — {e}")

    return result


def run(run_id: int, symbols: list[str] = None) -> list[dict]:
    """
    Scan all symbols and insert into symbol_universe.
    Returns list of result dicts.
    """
    symbols = symbols or SCAN_UNIVERSE
    max_price = FLOW["max_contract_price"]
    _log(f"Starting flow scan — {len(symbols)} symbols | run_id={run_id}")

    results = []
    for ticker in symbols:
        data = _scan_ticker(ticker, max_price)
        data["run_id"] = run_id
        universe_id = db.insert_universe_symbol(run_id, data)
        data["universe_id"] = universe_id
        results.append(data)
        time.sleep(FLOW["inter_ticker_delay"])

    flow_count = sum(1 for r in results if r["flow_detected"])
    _log(f"Scan complete — {flow_count}/{len(symbols)} symbols with unusual flow")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEXUS flow scanner")
    parser.add_argument("--run-id", type=int, help="Existing run ID to attach to")
    parser.add_argument("--symbols", type=str, help="Comma-separated symbol override")
    args = parser.parse_args()

    run_id = args.run_id or db.create_run(phase=1, notes="flow_scanner standalone")
    symbols = args.symbols.split(",") if args.symbols else None

    run(run_id, symbols)
