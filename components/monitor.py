"""
components/monitor.py — MONITOR: Position watcher (Phase 5)

Checks all open trades against current market prices and determines
whether stop or target levels have been hit.

In sim mode (default): closes positions in DB without Tastytrade orders.
With auto_close=False: logs findings only.

Standalone:
  python -m components.monitor                  # check + log only
  python -m components.monitor --auto-close     # close on hit (sim)
"""

import argparse
import re
import time
from datetime import date, datetime

import yfinance as yf

from shared import db
from shared.config import RISK


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [monitor] {msg}")


# Cache: contract_symbol → (price, monotonic_time)
_price_cache: dict[str, tuple[float, float]] = {}
# Cache: (symbol, expiry) → (chain_df_calls, chain_df_puts, monotonic_time)
_chain_cache: dict[tuple, tuple] = {}
_PRICE_TTL = 30.0   # reuse option price for 30s
_CHAIN_TTL = 120.0  # reuse full option chain for 2 min


def _parse_occ(contract_symbol: str) -> dict | None:
    """
    Parse OCC option symbol e.g. 'AVGO261003C02000000'
    → {symbol, expiry (YYYY-MM-DD), option_type (call/put), strike (float)}
    """
    m = re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})$', contract_symbol.strip())
    if not m:
        return None
    sym, yymmdd, opt, strike_raw = m.groups()
    try:
        exp = date(int("20" + yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
    except ValueError:
        return None
    return {
        "symbol":      sym,
        "expiry":      str(exp),
        "option_type": "call" if opt == "C" else "put",
        "strike":      int(strike_raw) / 1000.0,
    }


def _get_chain(symbol: str, expiry: str):
    """Return (calls_df, puts_df) from yfinance with chain-level cache."""
    now = time.monotonic()
    key = (symbol, expiry)
    cached = _chain_cache.get(key)
    if cached and (now - cached[2]) < _CHAIN_TTL:
        return cached[0], cached[1]
    try:
        chain = yf.Ticker(symbol).option_chain(expiry)
        _chain_cache[key] = (chain.calls, chain.puts, now)
        return chain.calls, chain.puts
    except Exception:
        return None, None


def _get_current_price(contract_symbol: str) -> float | None:
    """
    Return mid price for an option contract via yfinance.
    Uses a 30s per-contract cache to avoid hammering yfinance at 1s intervals.
    """
    now = time.monotonic()
    cached = _price_cache.get(contract_symbol)
    if cached and (now - cached[1]) < _PRICE_TTL:
        return cached[0]

    info = _parse_occ(contract_symbol)
    if not info:
        return None

    calls, puts = _get_chain(info["symbol"], info["expiry"])
    df = calls if info["option_type"] == "call" else puts
    if df is None or df.empty:
        return None

    # Match by contractSymbol first, then by strike proximity
    row = df[df["contractSymbol"] == contract_symbol]
    if row.empty:
        row = df[abs(df["strike"] - info["strike"]) < 0.01]
    if row.empty:
        return None

    r = row.iloc[0]
    bid = float(r.get("bid", 0) or 0)
    ask = float(r.get("ask", 0) or 0)
    if bid > 0 and ask > 0:
        price = round((bid + ask) / 2, 2)
    else:
        price = float(r.get("lastPrice", 0) or 0) or None

    if price is not None:
        _price_cache[contract_symbol] = (price, now)
    return price


def check_all(auto_close: bool = False, sim: bool = True) -> list[dict]:
    """
    Check all open trades against current market prices.
    sim=True (default): close positions in DB only, no Tastytrade orders.
    auto_close=False: log findings only.
    """
    open_trades = db.get_open_trades()
    if not open_trades:
        return []

    results = []
    for trade in open_trades:
        trade_id      = trade["id"]
        contract_sym  = trade["contract_symbol"]
        entry_price   = float(trade["entry_price"] or 0)
        stop_price    = float(trade["stop_price"]  or 0)
        target_price  = float(trade["target_price"] or 0)
        contracts     = int(trade["contracts"] or 1)
        status        = trade["status"]

        current_price = _get_current_price(contract_sym)

        if current_price is None:
            results.append({"trade_id": trade_id, "contract_symbol": contract_sym, "action": "NO_PRICE"})
            continue

        pnl_per_contract = (current_price - entry_price) * 100
        pnl_total        = pnl_per_contract * contracts
        pnl_pct = (current_price - entry_price) / entry_price * 100 if entry_price else 0

        action = "HOLD"
        exit_reason = None

        if stop_price and current_price <= stop_price:
            action = "STOP_HIT"
            exit_reason = "stop_hit"
        elif target_price and current_price >= target_price:
            action = "TARGET_HIT"
            exit_reason = "target_hit"

        indicator = "🔴" if action == "STOP_HIT" else ("🟢" if action == "TARGET_HIT" else "⚪")
        _log(f"  {indicator} {contract_sym:<36} "
             f"cur=${current_price:.2f}  "
             f"entry=${entry_price:.2f}  "
             f"stop=${stop_price:.2f}  "
             f"target=${target_price:.2f}  "
             f"pnl=${pnl_total:+.0f} ({pnl_pct:+.1f}%)  "
             f"{action}")

        if action != "HOLD" and auto_close and status == "open":
            # Apply exit slippage: simulate worse fill (bid side, execution delay)
            slippage = 1 - RISK["sim_exit_slippage_pct"] / 100
            exit_price = round(current_price * slippage, 2)
            exit_pnl   = round((exit_price - entry_price) * 100 * contracts, 2)
            exit_pct   = round((exit_price - entry_price) / entry_price * 100 if entry_price else 0, 4)
            db.close_trade(
                trade_id,
                exit_price=exit_price,
                pnl=exit_pnl,
                pnl_pct=exit_pct,
                exit_reason=exit_reason,
            )
            _log(f"    SIM CLOSED {contract_sym} x{contracts} "
                 f"@ ${exit_price:.2f} (mid=${current_price:.2f} -{RISK['sim_exit_slippage_pct']}% slippage)  "
                 f"pnl=${exit_pnl:+.0f}")

        results.append({
            "trade_id":        trade_id,
            "contract_symbol": contract_sym,
            "symbol":          trade.get("symbol", ""),
            "current_price":   current_price,
            "entry_price":     entry_price,
            "stop_price":      stop_price,
            "target_price":    target_price,
            "pnl":             round(pnl_total, 2),
            "pnl_pct":         round(pnl_pct, 2),
            "action":          action,
        })

    return results


def summary() -> dict:
    """Return P&L summary across all closed trades."""
    history = db.get_trade_history(limit=200)
    if not history:
        return {"trades": 0, "winners": 0, "losers": 0, "total_pnl": 0, "win_rate": 0}
    winners = [t for t in history if (t["pnl"] or 0) > 0]
    losers  = [t for t in history if (t["pnl"] or 0) <= 0]
    total   = sum(float(t["pnl"] or 0) for t in history)
    return {
        "trades":    len(history),
        "winners":   len(winners),
        "losers":    len(losers),
        "total_pnl": round(total, 2),
        "win_rate":  round(len(winners) / len(history) * 100, 1) if history else 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEXUS MONITOR position watcher")
    parser.add_argument("--auto-close", action="store_true",
                        help="Close positions when stop/target hit (sim mode, no Tastytrade)")
    args = parser.parse_args()
    results = check_all(auto_close=args.auto_close, sim=True)
    stats   = summary()
    print(f"\nMONITOR: {len(results)} position(s) checked")
    print(f"P&L Summary: {stats}")
