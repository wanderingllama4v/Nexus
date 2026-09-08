"""
shared/tastytrade_client.py — Tastytrade session + DXFeed client

Provides:
  - Tastytrade OAuth v13 session (singleton)
  - get_equity_quote(ticker) / get_equity_candles(ticker) via yfinance
  - get_option_expirations(ticker) — list of YYYY-MM-DD strings
  - get_option_chain_structure(ticker, expiry) — strikes + streamer symbols
  - get_dxfeed_data(streamer_symbols) — Quote + Greeks + Summary + Trade per contract
  - place_order / get_account_balance (Phase 5)
"""

import os
import asyncio
import threading
import time as _time
from datetime import date, datetime
from dotenv import load_dotenv

load_dotenv()

TT_SECRET    = os.getenv("TT_SECRET", "")
TT_REFRESH   = os.getenv("TT_REFRESH", "")
TT_ACCOUNT_ID = os.getenv("TT_ACCOUNT_ID", "")
TT_PAPER     = os.getenv("TT_PAPER", "false").lower() == "true"

_YF_MAP = {"SPX": "^GSPC", "NDX": "^NDX", "VIX": "^VIX", "RUT": "^RUT", "DJI": "^DJI"}


def _yf(ticker: str) -> str:
    return _YF_MAP.get(ticker.upper(), ticker)


def is_mock_mode() -> bool:
    return not (TT_SECRET and TT_REFRESH)


# ── Background async event loop (DXFeed is async) ────────────────────────────

_bg_loop = asyncio.new_event_loop()
threading.Thread(target=_bg_loop.run_forever, daemon=True, name="tt-async").start()


def _run(coro, timeout: float = 15):
    return asyncio.run_coroutine_threadsafe(coro, _bg_loop).result(timeout)


# ── Session singleton ─────────────────────────────────────────────────────────

_session = None
_session_lock = threading.Lock()


def get_session():
    global _session
    if _session is None:
        with _session_lock:
            if _session is None:
                if is_mock_mode():
                    raise RuntimeError(
                        "TT_SECRET and TT_REFRESH must be set in .env to use Tastytrade"
                    )
                from tastytrade import Session
                _session = Session(
                    provider_secret=TT_SECRET,
                    refresh_token=TT_REFRESH,
                    is_test=TT_PAPER,
                )
                print(f"[tastytrade] Session created (paper={TT_PAPER})")
    return _session


# ── Equity data via yfinance ──────────────────────────────────────────────────

def get_equity_quote(ticker: str) -> dict | None:
    """Real-time equity quote. Returns {ticker, price, prev_close, change_pct} or None."""
    try:
        import yfinance as yf
        info = yf.Ticker(_yf(ticker)).fast_info
        price = getattr(info, "last_price", None)
        prev  = getattr(info, "previous_close", None)
        if not price:
            return None
        price = float(price)
        prev  = float(prev) if prev else price
        return {
            "ticker":     ticker,
            "price":      price,
            "prev_close": prev,
            "change_pct": round((price - prev) / prev * 100, 4) if prev else 0.0,
        }
    except Exception as e:
        print(f"[tastytrade] get_equity_quote {ticker}: {e}")
        return None


def get_equity_candles(ticker: str, interval: str = "5min", days: int = 5) -> list:
    """OHLCV candles via yfinance. Returns list of {time, open, high, low, close, volume}."""
    interval_map = {
        "1min": "1m", "5min": "5m", "15min": "15m", "1hour": "1h", "daily": "1d",
    }
    yf_interval = interval_map.get(interval, "5m")
    try:
        import yfinance as yf
        import pandas as pd
        df = yf.download(
            _yf(ticker), period=f"{days}d", interval=yf_interval,
            progress=False, auto_adjust=True,
        )
        if df.empty:
            return []
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        candles = []
        for idx, row in df.iterrows():
            try:
                candles.append({
                    "time":   str(idx),
                    "open":   float(row["open"]),
                    "high":   float(row["high"]),
                    "low":    float(row["low"]),
                    "close":  float(row["close"]),
                    "volume": int(row.get("volume", 0) or 0),
                })
            except Exception:
                continue
        return candles
    except Exception as e:
        print(f"[tastytrade] get_equity_candles {ticker}: {e}")
        return []


# ── Option chain structure ────────────────────────────────────────────────────

def get_option_expirations(ticker: str) -> list[str]:
    """
    Return sorted list of available expiry dates as YYYY-MM-DD strings.
    Uses Tastytrade NestedOptionChain (sync in SDK v13).
    """
    if is_mock_mode():
        today = date.today()
        from datetime import timedelta
        return [
            (today + timedelta(days=d)).strftime("%Y-%m-%d")
            for d in [7, 14, 21, 35]
        ]
    try:
        from tastytrade.instruments import NestedOptionChain
        session = get_session()
        chains = _run(NestedOptionChain.get(session, ticker))
        today = date.today()
        dates = []
        for chain in (chains if isinstance(chains, list) else [chains]):
            for exp in chain.expirations:
                exp_date = exp.expiration_date
                if isinstance(exp_date, str):
                    exp_date = date.fromisoformat(exp_date)
                if exp_date >= today:
                    dates.append(str(exp_date))
        return sorted(set(dates))
    except Exception as e:
        print(f"[tastytrade] get_option_expirations {ticker}: {e}")
        return []


def get_option_chain_structure(ticker: str, expiry: str) -> list[dict]:
    """
    Return all strikes for a given expiry as:
      [{strike, call_occ, put_occ, call_streamer, put_streamer}, ...]
    No market data — just the chain skeleton needed for DXFeed subscriptions.
    """
    if is_mock_mode():
        return []
    try:
        from tastytrade.instruments import NestedOptionChain
        session = get_session()
        chains = _run(NestedOptionChain.get(session, ticker))
        strikes = []
        for chain in (chains if isinstance(chains, list) else [chains]):
            for exp in chain.expirations:
                if str(exp.expiration_date) != expiry:
                    continue
                for s in exp.strikes:
                    strikes.append({
                        "strike":         float(s.strike_price),
                        "call_occ":       s.call or "",
                        "put_occ":        s.put  or "",
                        "call_streamer":  s.call_streamer_symbol or "",
                        "put_streamer":   s.put_streamer_symbol  or "",
                    })
        return strikes
    except Exception as e:
        print(f"[tastytrade] get_option_chain_structure {ticker} {expiry}: {e}")
        return []


# ── DXFeed market data ────────────────────────────────────────────────────────

async def _fetch_dxfeed(streamer_symbols: list[str], timeout: float = 6.0) -> dict:
    """
    Subscribe to Quote, Greeks, Summary, and Trade for all streamer symbols.
    Uses asyncio.timeout() (Python 3.11+) so cancellation is clean and the
    DXLinkStreamer's internal TaskGroup doesn't raise ExceptionGroup on timeout.

    Returns {streamer_symbol: {bid, ask, delta, gamma, theta, vega, iv,
                               open_interest, day_volume, last_price}}
    """
    from tastytrade import DXLinkStreamer
    from tastytrade.dxfeed import Quote, Greeks, Summary, Trade

    session = get_session()
    results: dict = {s: {} for s in streamer_symbols}
    remaining = {
        "quote":   set(streamer_symbols),
        "greeks":  set(streamer_symbols),
        "summary": set(streamer_symbols),
        "trade":   set(streamer_symbols),
    }

    def _all_done() -> bool:
        return all(len(v) == 0 for v in remaining.values())

    stop = asyncio.Event()

    async def drain(event_type, key):
        async for ev in streamer.listen(event_type):
            sym = ev.event_symbol
            if sym in results:
                if key == "quote":
                    results[sym]["bid"] = float(ev.bid_price or 0)
                    results[sym]["ask"] = float(ev.ask_price or 0)
                elif key == "greeks":
                    results[sym]["delta"]      = float(ev.delta      or 0)
                    results[sym]["gamma"]      = float(ev.gamma      or 0)
                    results[sym]["theta"]      = float(ev.theta      or 0)
                    results[sym]["vega"]       = float(ev.vega       or 0)
                    results[sym]["iv"]         = float(ev.volatility or 0)
                    results[sym]["last_price"] = float(ev.price      or 0)
                elif key == "summary":
                    results[sym]["open_interest"] = int(ev.open_interest or 0)
                elif key == "trade":
                    results[sym]["day_volume"] = int(ev.day_volume or 0)
                remaining[key].discard(sym)
            if _all_done() or stop.is_set():
                break

    try:
        async with asyncio.timeout(timeout):
            async with DXLinkStreamer(session) as streamer:
                await streamer.subscribe(Quote,   list(streamer_symbols))
                await streamer.subscribe(Greeks,  list(streamer_symbols))
                await streamer.subscribe(Summary, list(streamer_symbols))
                await streamer.subscribe(Trade,   list(streamer_symbols))
                await asyncio.gather(
                    drain(Quote,   "quote"),
                    drain(Greeks,  "greeks"),
                    drain(Summary, "summary"),
                    drain(Trade,   "trade"),
                )
    except TimeoutError:
        stop.set()  # signal drains to exit on next event
    except Exception as e:
        # ExceptionGroup (from DXLinkStreamer's internal TaskGroup) is a subclass of Exception
        inner = getattr(e, 'exceptions', None)
        msg = str(inner[0]) if inner else str(e)
        print(f"[tastytrade] DXFeed fetch error: {msg}")

    return results


def get_dxfeed_data(streamer_symbols: list[str], timeout: float = 6.0) -> dict:
    """
    Sync wrapper around _fetch_dxfeed.
    Returns {streamer_symbol: {bid, ask, delta, gamma, theta, vega, iv,
                               open_interest, day_volume, last_price}}
    """
    if not streamer_symbols:
        return {}
    if is_mock_mode():
        import random
        return {
            s: {
                "bid": round(random.uniform(0.5, 3.0), 2),
                "ask": round(random.uniform(0.5, 3.0) + 0.05, 2),
                "delta": round(random.uniform(0.30, 0.70), 3),
                "gamma": round(random.uniform(0.01, 0.05), 4),
                "theta": round(random.uniform(-0.10, -0.01), 4),
                "vega":  round(random.uniform(0.05, 0.30), 4),
                "iv":    round(random.uniform(0.20, 0.80), 4),
                "last_price":    round(random.uniform(0.5, 3.0), 2),
                "open_interest": random.randint(200, 50000),
                "day_volume":    random.randint(50, 10000),
            }
            for s in streamer_symbols
        }
    try:
        # Use _fetch_dxfeed's own asyncio.timeout — no outer timeout needed
        return _run(_fetch_dxfeed(streamer_symbols, timeout), timeout=timeout + 10)
    except Exception as e:
        print(f"[tastytrade] get_dxfeed_data error: {e}")
        return {}


# ── Order execution (Phase 5) ─────────────────────────────────────────────────

def place_order(contract_occ: str, side: str, quantity: int) -> str | None:
    """
    Place a market options order.
    side: 'buy_to_open' | 'sell_to_close'
    Returns order_id string or None.
    """
    if is_mock_mode():
        print(f"[tastytrade][MOCK] place_order {contract_occ} {side} x{quantity}")
        return "mock_order_123"
    try:
        from tastytrade.instruments import NestedOptionChain
        from tastytrade.order import (
            MarketOrder, Leg, OrderAction, OrderTimeInForce, InstrumentType
        )
        session = get_session()
        from tastytrade import Account
        account_list = _run(Account.get(session, TT_ACCOUNT_ID) if TT_ACCOUNT_ID else Account.get(session))
        account = account_list[0] if isinstance(account_list, list) else account_list

        action_map = {
            "buy_to_open":   OrderAction.BUY_TO_OPEN,
            "sell_to_close": OrderAction.SELL_TO_CLOSE,
            "buy_to_close":  OrderAction.BUY_TO_CLOSE,
            "sell_to_open":  OrderAction.SELL_TO_OPEN,
        }
        order = MarketOrder(
            time_in_force=OrderTimeInForce.DAY,
            legs=[Leg(
                instrument_type=InstrumentType.EQUITY_OPTION,
                symbol=contract_occ,
                action=action_map.get(side.lower(), OrderAction.BUY_TO_OPEN),
                quantity=quantity,
            )],
        )
        response = _run(account.place_order(session, order, dry_run=False))
        if response and response.order:
            return str(response.order.id)
        return None
    except Exception as e:
        print(f"[tastytrade] place_order error: {e}")
        return None


def get_account_balance() -> tuple[float, float]:
    """Returns (cash_available, pending_cash) or (None, None)."""
    if is_mock_mode():
        return 5000.0, 0.0
    try:
        from tastytrade import Account
        session = get_session()
        account_list = _run(Account.get(session, TT_ACCOUNT_ID) if TT_ACCOUNT_ID else Account.get(session))
        account = account_list[0] if isinstance(account_list, list) else account_list
        bal = _run(account.get_balances(session))
        available = float(bal.cash_available_to_withdraw or bal.cash_balance or 0)
        pending   = float(bal.pending_cash or 0)
        return available, pending
    except Exception as e:
        print(f"[tastytrade] get_account_balance error: {e}")
        return None, None


def get_account_info() -> dict:
    """
    Returns {net_liq, cash, buying_power} for risk sizing.
    net_liq is total account value (cash + positions).
    """
    if is_mock_mode():
        return {"net_liq": 50_000.0, "cash": 25_000.0, "buying_power": 25_000.0}
    try:
        from tastytrade import Account
        session = get_session()
        account_list = _run(Account.get(session, TT_ACCOUNT_ID) if TT_ACCOUNT_ID else Account.get(session))
        account = account_list[0] if isinstance(account_list, list) else account_list
        bal = _run(account.get_balances(session))
        net_liq = float(
            getattr(bal, "net_liquidating_value", None)
            or getattr(bal, "net_liq", None)
            or getattr(bal, "cash_balance", 0)
            or 0
        )
        cash = float(getattr(bal, "cash_balance", 0) or 0)
        buying_power = float(
            getattr(bal, "derivative_buying_power", None)
            or getattr(bal, "cash_available_to_withdraw", None)
            or 0
        )
        return {"net_liq": net_liq, "cash": cash, "buying_power": buying_power}
    except Exception as e:
        print(f"[tastytrade] get_account_info error: {e}")
        return {"net_liq": 0.0, "cash": 0.0, "buying_power": 0.0}


def get_positions() -> list[dict]:
    """
    Returns open equity option positions.
    Each entry: {symbol, quantity, direction, avg_price, unrealized_pnl}
    Returns [] if no positions or on error.
    """
    if is_mock_mode():
        return []
    try:
        from tastytrade import Account
        session = get_session()
        account_list = _run(Account.get(session, TT_ACCOUNT_ID) if TT_ACCOUNT_ID else Account.get(session))
        account = account_list[0] if isinstance(account_list, list) else account_list
        positions = _run(account.get_positions(session))
        result = []
        for p in positions:
            itype = str(getattr(p, "instrument_type", "")).replace("_", " ").upper()
            if "OPTION" not in itype:
                continue
            result.append({
                "symbol":         str(getattr(p, "symbol", "")),
                "quantity":       int(getattr(p, "quantity", 0)),
                "direction":      str(getattr(p, "quantity_direction", "Long")),
                "avg_price":      float(getattr(p, "average_open_price", 0) or 0),
                "unrealized_pnl": float(getattr(p, "unrealized_day_gain_loss", 0) or 0),
            })
        return result
    except Exception as e:
        print(f"[tastytrade] get_positions error: {e}")
        return []
