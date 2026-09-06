"""
components/quant.py — Technical analysis engine

Reads symbol_universe for a run_id, fetches candles via yfinance,
runs all indicators, scores each symbol 0-100, writes to symbol_scans.

Standalone usage:
  python -m components.quant --run-id 42
  python -m components.quant --run-id 42 --symbol NVDA
"""

import argparse
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np

from shared import db
from shared.config import QUANT
from shared.tastytrade_client import get_equity_candles, get_equity_quote
from quant_lib.indicators import rsi, ema, atr, vwap, momentum, volume_ratio, relative_strength
from quant_lib.iv_engine import atm_iv_from_chain, expected_move, iv_percentile, skew


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [quant] {msg}")


@dataclass
class QuantResult:
    symbol: str
    price: float
    direction: str
    technical_score: float
    rsi: float
    atr: float
    atr_pct: float
    vwap: float
    vwap_position: str
    ema9: float
    ema21: float
    ema50: float
    ema_aligned: bool
    volume_ratio: float
    momentum_pct: float
    relative_strength: float
    atm_iv: float
    iv_percentile: float
    expected_move_1w: float
    skew_put_call: float


def _score(
    rsi_v: float, price: float, vwap_v: float,
    ema9_v: float, ema21_v: float, ema50_v: float,
    vol_ratio_v: float, mom_v: float, rel_str_v: float,
) -> float:
    """Weighted score 0-100. See shared/config.py for threshold tuning."""
    s = 0.0

    # RSI (20 pts) — 50-70 is the bullish sweet spot
    if 50 <= rsi_v <= 70:
        s += 20
    elif 45 <= rsi_v < 50 or 70 < rsi_v <= 75:
        s += 12
    elif 40 <= rsi_v < 45 or 75 < rsi_v <= 80:
        s += 6

    # VWAP position (15 pts)
    if price > vwap_v:
        s += 15
    elif price > vwap_v * 0.995:
        s += 8

    # EMA alignment (20 pts)
    if ema9_v > ema21_v > ema50_v:
        s += 20
    elif ema9_v > ema21_v:
        s += 12
    elif ema9_v > ema50_v:
        s += 6

    # Volume ratio (15 pts)
    if vol_ratio_v >= 2.0:
        s += 15
    elif vol_ratio_v >= 1.5:
        s += 12
    elif vol_ratio_v >= 1.2:
        s += 8
    elif vol_ratio_v >= 1.0:
        s += 5

    # Momentum (15 pts)
    if mom_v >= 0.03:
        s += 15
    elif mom_v >= 0.015:
        s += 10
    elif mom_v >= 0.005:
        s += 5
    elif mom_v >= 0:
        s += 2

    # Relative strength vs SPY (15 pts)
    if rel_str_v >= 0.02:
        s += 15
    elif rel_str_v >= 0.01:
        s += 10
    elif rel_str_v >= 0.005:
        s += 6
    elif rel_str_v >= 0:
        s += 3

    return round(s, 1)


def analyze(symbol: str, spy_candles: list = None, iv_hist: list = None) -> Optional[QuantResult]:
    """
    Run full technical analysis on a single symbol.
    Returns QuantResult or None if data is insufficient.
    """
    candles = get_equity_candles(
        symbol,
        interval=QUANT["candle_interval"],
        days=QUANT["candle_days"],
    )
    if len(candles) < QUANT["min_candles"]:
        _log(f"{symbol}: only {len(candles)} candles — skipping")
        return None

    quote = get_equity_quote(symbol)
    price = float(quote["price"]) if quote else float(candles[-1]["close"])

    closes  = np.array([c["close"]  for c in candles], dtype=float)
    highs   = np.array([c["high"]   for c in candles], dtype=float)
    lows    = np.array([c["low"]    for c in candles], dtype=float)
    volumes = np.array([c["volume"] for c in candles], dtype=float)

    rsi_v      = rsi(closes, QUANT["rsi_period"])
    atr_v      = atr(highs, lows, closes, QUANT["atr_period"])
    atr_pct_v  = atr_v / price * 100 if price else 0.0
    vwap_v     = vwap(highs, lows, closes, volumes)
    ema9_arr   = ema(closes, QUANT["ema_short"])
    ema21_arr  = ema(closes, QUANT["ema_mid"])
    ema50_arr  = ema(closes, QUANT["ema_long"])
    ema9_v     = float(ema9_arr[-1])
    ema21_v    = float(ema21_arr[-1])
    ema50_v    = float(ema50_arr[-1])
    vol_ratio_v = volume_ratio(volumes, QUANT["volume_avg_period"])
    mom_v      = momentum(closes, QUANT["momentum_period"])

    spy_closes = None
    if spy_candles and len(spy_candles) >= QUANT["momentum_period"] + 1:
        spy_closes = np.array([c["close"] for c in spy_candles], dtype=float)
    rel_str_v = relative_strength(closes, spy_closes) if spy_closes is not None else 0.0

    score = _score(rsi_v, price, vwap_v, ema9_v, ema21_v, ema50_v,
                   vol_ratio_v, mom_v, rel_str_v)

    if score >= QUANT["bullish_score_threshold"]:
        direction = "BULLISH"
    elif score <= QUANT["bearish_score_threshold"]:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    # IV context — fetched lazily (no option chain call here; uses stored iv_hist)
    iv_pct_v = iv_percentile(0.0, iv_hist or [])

    return QuantResult(
        symbol=symbol,
        price=round(price, 4),
        direction=direction,
        technical_score=score,
        rsi=round(rsi_v, 1),
        atr=round(atr_v, 4),
        atr_pct=round(atr_pct_v, 3),
        vwap=round(vwap_v, 4),
        vwap_position="ABOVE" if price > vwap_v else "BELOW",
        ema9=round(ema9_v, 4),
        ema21=round(ema21_v, 4),
        ema50=round(ema50_v, 4),
        ema_aligned=bool(ema9_v > ema21_v > ema50_v),
        volume_ratio=round(vol_ratio_v, 2),
        momentum_pct=round(mom_v * 100, 3),
        relative_strength=round(rel_str_v * 100, 3),
        atm_iv=0.0,
        iv_percentile=round(iv_pct_v, 1),
        expected_move_1w=0.0,
        skew_put_call=0.0,
    )


def run(run_id: int, symbol_filter: str = None):
    """
    Process all symbols in symbol_universe for run_id.
    Writes results to symbol_scans. Returns list of QuantResult.
    """
    universe = db.get_universe(run_id)
    if not universe:
        _log(f"No symbols in universe for run_id={run_id}")
        return []

    if symbol_filter:
        universe = [u for u in universe if u["symbol"] == symbol_filter.upper()]

    _log(f"Analysing {len(universe)} symbols | run_id={run_id}")

    # Fetch SPY once as benchmark
    spy_candles = get_equity_candles("SPY", interval=QUANT["candle_interval"],
                                      days=QUANT["candle_days"])
    _log(f"SPY benchmark: {len(spy_candles)} candles")

    results = []
    for row in universe:
        symbol      = row["symbol"]
        universe_id = row["id"]
        iv_hist     = db.get_iv_history(symbol, days=252)

        result = analyze(symbol, spy_candles=spy_candles, iv_hist=iv_hist)
        if result is None:
            continue

        scan_id = db.insert_symbol_scan(run_id, universe_id, {
            "symbol":           result.symbol,
            "price":            result.price,
            "direction":        result.direction,
            "technical_score":  result.technical_score,
            "rsi":              result.rsi,
            "atr":              result.atr,
            "atr_pct":          result.atr_pct,
            "vwap":             result.vwap,
            "vwap_position":    result.vwap_position,
            "ema9":             result.ema9,
            "ema21":            result.ema21,
            "ema50":            result.ema50,
            "ema_aligned":      result.ema_aligned,
            "volume_ratio":     result.volume_ratio,
            "momentum_pct":     result.momentum_pct,
            "relative_strength":result.relative_strength,
            "atm_iv":           result.atm_iv,
            "iv_percentile":    result.iv_percentile,
            "expected_move_1w": result.expected_move_1w,
            "skew_put_call":    result.skew_put_call,
        })

        vwap_arrow = "▲" if result.vwap_position == "ABOVE" else "▼"
        ema_str    = "✓✓✓" if result.ema_aligned else ("✓✓" if result.ema9 > result.ema21 else "✗")
        _log(f"{symbol:<6} score={result.technical_score:>5.1f}  "
             f"{result.direction:<8}  RSI={result.rsi:.1f}  "
             f"VWAP:{vwap_arrow}  EMA:{ema_str}  "
             f"Vol:{result.volume_ratio:.1f}x  RS:{result.relative_strength:+.2f}%  "
             f"[scan_id={scan_id}]")

        results.append(result)

    results.sort(key=lambda r: r.technical_score, reverse=True)
    _log(f"Done — {len(results)} symbols scored")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEXUS quant engine")
    parser.add_argument("--run-id", type=int, required=True, help="Run ID to process")
    parser.add_argument("--symbol", type=str, help="Analyse a single symbol only")
    args = parser.parse_args()

    run(args.run_id, symbol_filter=args.symbol)
