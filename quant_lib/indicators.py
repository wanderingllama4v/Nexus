"""
quant_lib/indicators.py — Pure numpy technical indicators

All functions accept numpy arrays and return scalars.
No external TA library dependency — deterministic, testable.
"""

import numpy as np


def rsi(closes: np.ndarray, period: int = 14) -> float:
    """Wilder's RSI. Returns 50.0 if insufficient data."""
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes.astype(float))
    gains  = np.where(deltas > 0, deltas,  0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return float(100 - (100 / (1 + rs)))


def ema(closes: np.ndarray, period: int) -> np.ndarray:
    """Exponential moving average. Returns array same length as input."""
    closes = closes.astype(float)
    k = 2.0 / (period + 1)
    result = np.empty(len(closes))
    result[0] = closes[0]
    for i in range(1, len(closes)):
        result[i] = closes[i] * k + result[i - 1] * (1 - k)
    return result


def atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, period: int = 14) -> float:
    """Wilder's ATR. Falls back to (high-low) range if insufficient data."""
    highs  = highs.astype(float)
    lows   = lows.astype(float)
    closes = closes.astype(float)
    if len(closes) < 2:
        return float(highs[-1] - lows[-1])
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(
            np.abs(highs[1:] - closes[:-1]),
            np.abs(lows[1:]  - closes[:-1]),
        ),
    )
    if len(tr) < period:
        return float(np.mean(tr))
    atr_val = float(np.mean(tr[:period]))
    for i in range(period, len(tr)):
        atr_val = (atr_val * (period - 1) + tr[i]) / period
    return atr_val


def vwap(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray, volumes: np.ndarray) -> float:
    """Volume-weighted average price over the supplied candles."""
    highs   = highs.astype(float)
    lows    = lows.astype(float)
    closes  = closes.astype(float)
    volumes = volumes.astype(float)
    typical = (highs + lows + closes) / 3.0
    total_vol = float(np.sum(volumes))
    if total_vol == 0:
        return float(closes[-1])
    return float(np.sum(typical * volumes) / total_vol)


def momentum(closes: np.ndarray, period: int = 10) -> float:
    """Rate of change over `period` bars. Returns 0.0 if insufficient data."""
    closes = closes.astype(float)
    if len(closes) < period + 1:
        return 0.0
    base = closes[-(period + 1)]
    if base == 0:
        return 0.0
    return float((closes[-1] - base) / base)


def volume_ratio(volumes: np.ndarray, period: int = 20) -> float:
    """Current bar volume / average of previous `period` bars."""
    volumes = volumes.astype(float)
    if len(volumes) < 2:
        return 1.0
    hist = volumes[:-1]
    avg  = float(np.mean(hist[-period:])) if len(hist) >= period else float(np.mean(hist))
    if avg == 0:
        return 1.0
    return float(volumes[-1] / avg)


def relative_strength(sym_closes: np.ndarray, spy_closes: np.ndarray, period: int = 10) -> float:
    """
    Return - SPY return over `period` bars.
    Positive = outperforming SPY. Returns 0.0 if data is insufficient.
    """
    sym_closes = sym_closes.astype(float)
    spy_closes = spy_closes.astype(float)
    if len(sym_closes) < period + 1 or len(spy_closes) < period + 1:
        return 0.0
    sym_ret = (sym_closes[-1] - sym_closes[-(period + 1)]) / sym_closes[-(period + 1)]
    spy_ret = (spy_closes[-1] - spy_closes[-(period + 1)]) / spy_closes[-(period + 1)]
    return float(sym_ret - spy_ret)
