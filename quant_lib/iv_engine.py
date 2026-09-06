"""
quant_lib/iv_engine.py — IV percentile, expected move, skew, IV solver
"""

import numpy as np
from scipy.optimize import brentq
from quant_lib.greeks import bs_price


def expected_move(price: float, iv: float, dte: int) -> float:
    """1-sigma expected move: price × IV × sqrt(DTE / 365)."""
    if iv <= 0 or dte <= 0:
        return 0.0
    return float(price * iv * np.sqrt(dte / 365.0))


def iv_percentile(current_iv: float, history: list[float]) -> float:
    """
    Rank current_iv against historical observations.
    Returns 0-100. Requires at least 30 observations for meaningful output.
    """
    if not history or len(history) < 5:
        return 50.0
    arr = np.array(history, dtype=float)
    return float(np.mean(arr < current_iv) * 100)


def implied_volatility(
    market_price: float,
    S: float,
    K: float,
    T: float,
    r: float = 0.05,
    option_type: str = "call",
) -> float:
    """
    Solve for implied volatility using Brent's method.
    Returns 0.0 if no solution found (deep ITM/OTM, zero price, etc.).
    """
    if T <= 0 or market_price <= 0 or S <= 0 or K <= 0:
        return 0.0
    try:
        def objective(sigma):
            return bs_price(S, K, T, r, sigma, option_type) - market_price
        # Intrinsic value check — can't solve if market price < intrinsic
        intrinsic = max(S - K, 0) if option_type == "call" else max(K - S, 0)
        if market_price <= intrinsic:
            return 0.0
        return float(brentq(objective, 1e-4, 10.0, xtol=1e-6, maxiter=200))
    except (ValueError, RuntimeError):
        return 0.0


def atm_iv_from_chain(chain_data: list[dict], current_price: float) -> float:
    """
    Find the nearest ATM strike in chain_data and return its IV.
    chain_data entries must have keys: strike, iv (from DXFeed greeks).
    """
    if not chain_data:
        return 0.0
    atm = min(chain_data, key=lambda x: abs(float(x.get("strike", 0)) - current_price))
    return float(atm.get("iv") or 0.0)


def skew(chain_data: list[dict], target_delta: float = 0.25) -> dict:
    """
    Compare put IV vs call IV at the same absolute delta level.
    Returns {call_iv, put_iv, skew} where skew = put_iv - call_iv.
    Positive skew means puts are more expensive (bearish lean / fear).
    """
    calls = [c for c in chain_data if c.get("option_type", "").lower() == "call"]
    puts  = [c for c in chain_data if c.get("option_type", "").lower() == "put"]

    def nearest_iv(options, tgt):
        if not options:
            return 0.0
        best = min(options, key=lambda x: abs(abs(float(x.get("delta") or 0)) - tgt))
        return float(best.get("iv") or 0.0)

    call_iv = nearest_iv(calls, target_delta)
    put_iv  = nearest_iv(puts,  target_delta)
    return {
        "call_iv": call_iv,
        "put_iv":  put_iv,
        "skew":    round(put_iv - call_iv, 4),
    }
