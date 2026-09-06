"""
shared/config.py — NEXUS configuration

Symbol universe and scanner thresholds are defined here.
Add symbols to SCAN_UNIVERSE to expand coverage — no other file changes needed.
"""

# ── Symbol universe ───────────────────────────────────────────────────────────
# Starting set from OptionzAlertz flow_reader. Extend freely.
SCAN_UNIVERSE = [
    # Index ETFs
    "SPY", "QQQ", "IWM", "GLD", "XLK", "XLE",
    # Mega-cap tech
    "NVDA", "TSLA", "AAPL", "META", "AMZN", "MSFT", "GOOGL", "AMD",
    # High beta / momentum
    "PLTR", "SOFI", "MU", "COIN", "MSTR",
    # Biotech / pharma
    "MRNA", "BNTX", "LLY", "NVAX",
    # Additional semis / AI
    "AVGO", "ARM",
]

# Tickers treated as index ETFs for flow vol/OI thresholds
INDEX_TICKERS = {"SPY", "QQQ", "IWM", "GLD", "XLK", "XLE"}

# ── Flow scanner thresholds ────────────────────────────────────────────────────
FLOW = {
    "min_raw_volume":          500,       # minimum contract volume
    "min_dte":                 1,
    "max_dte":                 45,
    "atm_pct":                 0.04,      # strike within 4% of stock price
    "vol_oi_ratio_index":      20.0,      # index ETFs (high baseline volume)
    "vol_oi_ratio_stock":      5.0,       # individual stocks
    "min_premium_dollars":     200_000,   # notional: vol × mid × 100
    "max_contract_price":      300,       # skip options > $3.00/share
    "poll_interval_seconds":   900,       # 15 min between full scans
    "inter_ticker_delay":      2,         # seconds between ticker fetches
}

# ── Quant engine thresholds ────────────────────────────────────────────────────
QUANT = {
    "min_candles":             30,        # minimum candles needed to score
    "candle_interval":         "5min",
    "candle_days":             5,
    "rsi_period":              14,
    "atr_period":              14,
    "ema_short":               9,
    "ema_mid":                 21,
    "ema_long":                50,
    "momentum_period":         10,
    "volume_avg_period":       20,
    "bullish_score_threshold": 60,        # score >= 60 → BULLISH
    "bearish_score_threshold": 40,        # score <= 40 → BEARISH
    "top_symbols_to_scan":     5,         # pass top N to options scanner
}

# ── Options scanner thresholds ─────────────────────────────────────────────────
SCANNER = {
    "min_dte":          7,
    "max_dte":          45,
    "min_delta":        0.30,
    "max_delta":        0.70,
    "max_spread_pct":   4.0,    # (ask-bid)/mid × 100 — max 4%
    "min_oi":           200,
    "min_volume":       50,
    "max_iv":           1.50,   # skip contracts with IV > 150%
    "dxfeed_timeout":   6.0,    # seconds to wait for DXFeed events per batch
    "top_contracts":    5,      # contracts to keep per symbol
}

# ── Risk-free rate (used for Black-Scholes fallback in quant_lib) ──────────────
RISK_FREE_RATE = 0.05
