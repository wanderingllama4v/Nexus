"""
shared/universe.py — Dynamic scan universe for NEXUS.

Before 9:30 AM ET: uses the static CORE_TICKERS list only.
At 9:30 AM ET: fetches most_actives, day_gainers, day_losers from Yahoo Finance,
merges with core, caps at MAX_TOTAL_UNIVERSE. Refreshes every 30 minutes during
market hours so names that heat up mid-day get picked up automatically.

Call get_scan_universe() anywhere in the pipeline — always returns the current list.
Call start_universe_refresh() once at server startup to launch the background thread.
"""

import threading
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

from shared.config import SCAN_UNIVERSE as _CONFIG_CORE

ET = ZoneInfo("America/New_York")

# Always included — high-liquidity names from config with deep options markets.
CORE_TICKERS = list(_CONFIG_CORE)
_CORE_SET = set(CORE_TICKERS)

# Tickers to never add from dynamic feeds (no liquid options, foreign-listed, etc.)
_EXCLUDE = {"BRK.B", "BRK-B", "BRKB", "BF.B", "BF-B"}

_dynamic_tickers: list = []
_lock = threading.Lock()
_last_refresh: datetime | None = None

REFRESH_INTERVAL_MIN = 30
MAX_DYNAMIC_PER_SCREENER = 25
MAX_TOTAL_UNIVERSE = 75  # cap to keep scan time reasonable


def _log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [universe] {msg}", flush=True)


def _fetch_screener(scr_id: str, count: int = MAX_DYNAMIC_PER_SCREENER) -> list:
    try:
        r = requests.get(
            "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved",
            params={"scrIds": scr_id, "count": count, "formatted": "false"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        r.raise_for_status()
        quotes = (
            r.json()
            .get("finance", {})
            .get("result", [{}])[0]
            .get("quotes", [])
        )
        return [
            q["symbol"]
            for q in quotes
            if q.get("symbol")
            and "." not in q["symbol"]
            and q["symbol"] not in _EXCLUDE
            and float(q.get("regularMarketPrice") or 0) >= 2.0
        ]
    except Exception as e:
        _log(f"screener {scr_id} error: {e}")
        return []


def refresh_universe():
    global _dynamic_tickers, _last_refresh

    most_active = _fetch_screener("most_actives")
    gainers     = _fetch_screener("day_gainers")
    losers      = _fetch_screener("day_losers")

    # Deduplicate across screeners; most_actives gets priority (market focus)
    seen = set()
    combined = []
    for sym in most_active + gainers + losers:
        if sym not in seen and sym not in _CORE_SET:
            seen.add(sym)
            combined.append(sym)

    max_dynamic = MAX_TOTAL_UNIVERSE - len(CORE_TICKERS)
    combined = combined[:max_dynamic]

    with _lock:
        _dynamic_tickers = combined
        _last_refresh = datetime.now(ET)

    total = len(CORE_TICKERS) + len(combined)
    _log(
        f"Universe refreshed — {len(CORE_TICKERS)} core + {len(combined)} dynamic "
        f"= {total} tickers  "
        f"(active: {most_active[:5]}  gainers: {gainers[:3]}  losers: {losers[:3]})"
    )


def get_scan_universe() -> list:
    """Return the current combined ticker list. Always safe to call."""
    with _lock:
        dynamic = list(_dynamic_tickers)
    extras = [t for t in dynamic if t not in _CORE_SET]
    return CORE_TICKERS + extras


def get_universe_status() -> dict:
    """Return metadata about the current universe for API/dashboard use."""
    with _lock:
        dynamic = list(_dynamic_tickers)
        last = _last_refresh
    return {
        "core_count":    len(CORE_TICKERS),
        "dynamic_count": len(dynamic),
        "total_count":   len(CORE_TICKERS) + len(dynamic),
        "last_refresh":  last.isoformat() if last else None,
        "dynamic_tickers": dynamic,
    }


def _refresh_loop():
    while True:
        try:
            now = datetime.now(ET)
            market_open  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
            market_close = now.replace(hour=16, minute=0,  second=0, microsecond=0)
            in_hours = market_open <= now <= market_close and now.weekday() < 5

            if in_hours:
                stale = (
                    _last_refresh is None
                    or (now - _last_refresh).total_seconds() >= REFRESH_INTERVAL_MIN * 60
                )
                if stale:
                    try:
                        refresh_universe()
                    except Exception as e:
                        _log(f"Refresh error: {e}")
        except Exception as e:
            _log(f"Loop error: {e}")

        time.sleep(60)


def start_universe_refresh():
    t = threading.Thread(target=_refresh_loop, daemon=True, name="nexus-universe-refresh")
    t.start()
    _log(
        f"Universe refresh loop started — "
        f"{len(CORE_TICKERS)} core tickers, dynamic expansion at 9:30 AM ET "
        f"(max {MAX_TOTAL_UNIVERSE} total)"
    )
