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

# ── Sector ETFs (Phase 2: ATLAS + COMPASS) ────────────────────────────────────
SECTOR_ETFS = {
    "XLK":  "Technology",
    "SMH":  "Semiconductors",
    "XLC":  "Communication",
    "XLY":  "Consumer Discretionary",
    "XLF":  "Financials",
    "XLV":  "Healthcare",
    "XLI":  "Industrials",
    "XLE":  "Energy",
    "XLB":  "Materials",
    "XLRE": "Real Estate",
    "XLU":  "Utilities",
    "XLP":  "Consumer Staples",
    "GLD":  "Gold",
    "SPY":  "S&P 500",
    "QQQ":  "Nasdaq 100",
    "IWM":  "Russell 2000",
}

# Symbol → sector ETF mapping for COMPASS filtering
SYMBOL_SECTORS = {
    "NVDA":  ["XLK", "SMH"],
    "AMD":   ["XLK", "SMH"],
    "AVGO":  ["XLK", "SMH"],
    "ARM":   ["XLK", "SMH"],
    "MU":    ["XLK", "SMH"],
    "AAPL":  ["XLK"],
    "MSFT":  ["XLK"],
    "GOOGL": ["XLK", "XLC"],
    "META":  ["XLC"],
    "AMZN":  ["XLY", "XLK"],
    "TSLA":  ["XLY"],
    "PLTR":  ["XLK"],
    "SOFI":  ["XLF"],
    "COIN":  ["XLF"],
    "MSTR":  ["XLK"],
    "MRNA":  ["XLV"],
    "BNTX":  ["XLV"],
    "LLY":   ["XLV"],
    "NVAX":  ["XLV"],
    "SPY":   ["SPY"],
    "QQQ":   ["QQQ"],
    "IWM":   ["IWM"],
    "GLD":   ["GLD"],
    "XLK":   ["XLK"],
    "XLE":   ["XLE"],
}

# ── Risk management (Phase 4: SENTINEL + JUDGE + GUARDIAN) ───────────────────
RISK = {
    "risk_per_trade_pct":     1.5,   # % of account net_liq to risk per trade
    "max_total_new_risk_pct": 5.0,   # % cap across all new trades in one run
    "max_sector_risk_pct":    60.0,  # % of risk budget allowed in one sector
    "earnings_blackout_days": 1,     # reject option trades N days before earnings
    "min_account_net_liq":    100,   # paper account threshold (fund via TT paper reset for live)
    "sim_entry_slippage_pct": 0.5,   # sim entry: pay 0.5% above mid (realistic fill)
    "sim_exit_slippage_pct":  1.0,   # sim exit: receive 1.0% below mid (wider spread at close)
}

# ── LLM models ────────────────────────────────────────────────────────────────
LLM = {
    "atlas":   "gpt-4o",
    "compass": "gpt-4o",
    "hunter":  "gpt-4o",       # Phase 3
    "edge":    "gpt-4o",       # Phase 3
    "judge":   "gpt-4o",       # Phase 4
    "analyst": "claude-opus-4-8",  # Phase 3
    "sentinel":"claude-opus-4-8",  # Phase 4
    "scout":   "sonar-pro",    # Perplexity
}
