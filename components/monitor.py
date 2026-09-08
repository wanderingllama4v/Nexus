"""
components/monitor.py — MONITOR: Position watcher (Phase 5)

Checks all open trades against current market prices and determines
whether stop or target levels have been hit.

With auto_close=False (default): logs findings, no orders placed.
With auto_close=True: places sell_to_close orders when stop/target hit.

Standalone:
  python -m components.monitor                  # check + log only
  python -m components.monitor --auto-close     # check + close on hit
"""

import argparse
import json
import time
from datetime import date, datetime

from shared import db
from shared.tastytrade_client import get_equity_quote, get_dxfeed_data, place_order, TT_PAPER


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [monitor] {msg}")


# Price cache — refreshed at most every 2 seconds per contract.
# Prevents overlapping DXFeed calls when the monitor loop runs at 1s intervals.
_price_cache: dict[str, tuple[float, float]] = {}  # symbol -> (price, timestamp)
_CACHE_TTL = 2.0


def _get_current_price(contract_symbol: str, symbol: str) -> float | None:
    """
    Return mid price for an option contract, using a 2s cache to avoid
    overlapping DXFeed requests when called at 1-second intervals.
    """
    now = time.monotonic()
    cached = _price_cache.get(contract_symbol)
    if cached and (now - cached[1]) < _CACHE_TTL:
        return cached[0]

    price = None
    try:
        data = get_dxfeed_data([contract_symbol], timeout=4.0)
        d = data.get(contract_symbol, {})
        bid = d.get("bid", 0)
        ask = d.get("ask", 0)
        if bid and ask:
            price = round((bid + ask) / 2, 2)
        elif d.get("last_price"):
            price = float(d["last_price"])
    except Exception:
        pass

    if price is None:
        q = get_equity_quote(symbol)
        price = q["price"] if q else None

    if price is not None:
        _price_cache[contract_symbol] = (price, now)
    return price


def check_all(auto_close: bool = False) -> list[dict]:
    """
    Check all open trades. Returns list of status dicts.
    auto_close=True places sell_to_close orders when stop/target hit.
    """
    open_trades = db.get_open_trades()
    if not open_trades:
        _log("No open trades to monitor")
        return []

    _log(f"Checking {len(open_trades)} open trade(s) | auto_close={auto_close}")
    results = []

    for trade in open_trades:
        trade_id       = trade["id"]
        contract_sym   = trade["contract_symbol"]
        symbol         = trade["symbol"]
        entry_price    = float(trade["entry_price"] or 0)
        stop_price     = float(trade["stop_price"]  or 0)
        target_price   = float(trade["target_price"] or 0)
        contracts      = int(trade["contracts"] or 1)
        status         = trade["status"]

        current_price = _get_current_price(contract_sym, symbol)

        if current_price is None:
            _log(f"  {contract_sym}: price unavailable")
            results.append({"trade_id": trade_id, "status": "no_price"})
            continue

        pnl_per_contract = (current_price - entry_price) * 100
        pnl_total        = pnl_per_contract * contracts
        pnl_pct          = (current_price - entry_price) / entry_price * 100 if entry_price else 0
        current_val      = current_price * contracts * 100
        entry_val        = entry_price   * contracts * 100

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
            if not TT_PAPER:
                _log(f"    ⚠️  CAUTION: auto_close on LIVE account — skipping {contract_sym}")
                _log(f"    Use Tastytrade app to close manually.")
            else:
                order_id = place_order(contract_sym, "sell_to_close", contracts)
                if order_id:
                    db.close_trade(
                        trade_id,
                        exit_price=current_price,
                        pnl=round(pnl_total, 2),
                        pnl_pct=round(pnl_pct, 4),
                        exit_reason=exit_reason,
                    )
                    _log(f"    CLOSED {contract_sym} x{contracts} → order_id={order_id}")
                else:
                    _log(f"    CLOSE FAILED {contract_sym}")

        results.append({
            "trade_id":      trade_id,
            "contract_symbol": contract_sym,
            "symbol":        symbol,
            "current_price": current_price,
            "entry_price":   entry_price,
            "stop_price":    stop_price,
            "target_price":  target_price,
            "pnl":           round(pnl_total, 2),
            "pnl_pct":       round(pnl_pct, 2),
            "action":        action,
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
        "trades":   len(history),
        "winners":  len(winners),
        "losers":   len(losers),
        "total_pnl": round(total, 2),
        "win_rate":  round(len(winners) / len(history) * 100, 1) if history else 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEXUS MONITOR position watcher")
    parser.add_argument("--auto-close", action="store_true",
                        help="Close positions at stop/target (paper accounts only)")
    args = parser.parse_args()
    results = check_all(auto_close=args.auto_close)
    stats   = summary()
    print(f"\nMONITOR: {len(results)} position(s) checked")
    print(f"P&L Summary: {stats}")
