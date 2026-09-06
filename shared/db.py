"""
shared/db.py — PostgreSQL connection and helpers

All components import get_conn() for raw psycopg2 access.
The schema is created on first connect if tables don't exist.
"""

import os
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://nexus:nexus@localhost:5432/nexus")


def get_conn():
    """Return a new psycopg2 connection. Caller is responsible for closing."""
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)


@contextmanager
def cursor():
    """Context manager: yields a cursor, commits on exit, rolls back on error."""
    conn = get_conn()
    try:
        with conn:
            with conn.cursor() as cur:
                yield cur
    finally:
        conn.close()


def init_schema():
    """Run schema.sql against the DB. Safe to call multiple times (IF NOT EXISTS)."""
    schema_path = os.path.join(os.path.dirname(__file__), "..", "db", "schema.sql")
    schema_path = os.path.normpath(schema_path)
    with open(schema_path) as f:
        sql = f.read()
    with cursor() as cur:
        cur.execute(sql)
    print("[db] Schema initialised")


# ── Run helpers ───────────────────────────────────────────────────────────────

def create_run(phase: int = 1, notes: str = None) -> int:
    """Insert a new run row and return its id."""
    with cursor() as cur:
        cur.execute(
            "INSERT INTO runs (phase, notes) VALUES (%s, %s) RETURNING id",
            (phase, notes),
        )
        return cur.fetchone()["id"]


def complete_run(run_id: int, symbols_scanned: int, contracts_found: int):
    with cursor() as cur:
        cur.execute(
            """UPDATE runs
               SET status = 'completed', completed_at = NOW(),
                   symbols_scanned = %s, contracts_found = %s
               WHERE id = %s""",
            (symbols_scanned, contracts_found, run_id),
        )


def fail_run(run_id: int, notes: str = None):
    with cursor() as cur:
        cur.execute(
            "UPDATE runs SET status = 'failed', completed_at = NOW(), notes = %s WHERE id = %s",
            (notes, run_id),
        )


# ── Symbol universe helpers ───────────────────────────────────────────────────

def insert_universe_symbol(run_id: int, data: dict) -> int:
    """Insert one symbol into symbol_universe. Returns the new row id."""
    with cursor() as cur:
        cur.execute(
            """INSERT INTO symbol_universe
               (run_id, symbol, price, prev_close, change_pct,
                flow_detected, flow_direction, flow_strike, flow_expiry,
                flow_volume, flow_oi, flow_vol_oi_ratio, flow_notional, flow_confidence)
               VALUES
               (%(run_id)s, %(symbol)s, %(price)s, %(prev_close)s, %(change_pct)s,
                %(flow_detected)s, %(flow_direction)s, %(flow_strike)s, %(flow_expiry)s,
                %(flow_volume)s, %(flow_oi)s, %(flow_vol_oi_ratio)s, %(flow_notional)s, %(flow_confidence)s)
               RETURNING id""",
            {
                "run_id":           run_id,
                "symbol":           data["symbol"],
                "price":            data.get("price"),
                "prev_close":       data.get("prev_close"),
                "change_pct":       data.get("change_pct"),
                "flow_detected":    data.get("flow_detected", False),
                "flow_direction":   data.get("flow_direction"),
                "flow_strike":      data.get("flow_strike"),
                "flow_expiry":      data.get("flow_expiry"),
                "flow_volume":      data.get("flow_volume"),
                "flow_oi":          data.get("flow_oi"),
                "flow_vol_oi_ratio":data.get("flow_vol_oi_ratio"),
                "flow_notional":    data.get("flow_notional"),
                "flow_confidence":  data.get("flow_confidence"),
            },
        )
        return cur.fetchone()["id"]


def get_universe(run_id: int) -> list:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM symbol_universe WHERE run_id = %s ORDER BY id",
            (run_id,),
        )
        return cur.fetchall()


# ── Symbol scan helpers ───────────────────────────────────────────────────────

def insert_symbol_scan(run_id: int, universe_id: int, data: dict) -> int:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO symbol_scans
               (run_id, universe_id, symbol, price, direction, technical_score,
                rsi, atr, atr_pct, vwap, vwap_position, ema9, ema21, ema50,
                ema_aligned, volume_ratio, momentum_pct, relative_strength,
                atm_iv, iv_percentile, expected_move_1w, skew_put_call)
               VALUES
               (%(run_id)s, %(universe_id)s, %(symbol)s, %(price)s, %(direction)s, %(technical_score)s,
                %(rsi)s, %(atr)s, %(atr_pct)s, %(vwap)s, %(vwap_position)s,
                %(ema9)s, %(ema21)s, %(ema50)s, %(ema_aligned)s, %(volume_ratio)s,
                %(momentum_pct)s, %(relative_strength)s,
                %(atm_iv)s, %(iv_percentile)s, %(expected_move_1w)s, %(skew_put_call)s)
               RETURNING id""",
            {
                "run_id":           run_id,
                "universe_id":      universe_id,
                "symbol":           data["symbol"],
                "price":            data.get("price"),
                "direction":        data["direction"],
                "technical_score":  data["technical_score"],
                "rsi":              data.get("rsi"),
                "atr":              data.get("atr"),
                "atr_pct":          data.get("atr_pct"),
                "vwap":             data.get("vwap"),
                "vwap_position":    data.get("vwap_position"),
                "ema9":             data.get("ema9"),
                "ema21":            data.get("ema21"),
                "ema50":            data.get("ema50"),
                "ema_aligned":      data.get("ema_aligned"),
                "volume_ratio":     data.get("volume_ratio"),
                "momentum_pct":     data.get("momentum_pct"),
                "relative_strength":data.get("relative_strength"),
                "atm_iv":           data.get("atm_iv"),
                "iv_percentile":    data.get("iv_percentile"),
                "expected_move_1w": data.get("expected_move_1w"),
                "skew_put_call":    data.get("skew_put_call"),
            },
        )
        return cur.fetchone()["id"]


def get_symbol_scans(run_id: int) -> list:
    with cursor() as cur:
        cur.execute(
            "SELECT * FROM symbol_scans WHERE run_id = %s ORDER BY technical_score DESC",
            (run_id,),
        )
        return cur.fetchall()


# ── Contract scan helpers ─────────────────────────────────────────────────────

def insert_contract_scan(run_id: int, symbol_scan_id: int, data: dict) -> int:
    with cursor() as cur:
        cur.execute(
            """INSERT INTO contract_scans
               (run_id, symbol_scan_id, symbol, contract_symbol, expiry, dte,
                strike, option_type, bid, ask, mid, spread_pct,
                open_interest, day_volume, delta, gamma, theta, vega, iv,
                contract_score)
               VALUES
               (%(run_id)s, %(symbol_scan_id)s, %(symbol)s, %(contract_symbol)s,
                %(expiry)s, %(dte)s, %(strike)s, %(option_type)s,
                %(bid)s, %(ask)s, %(mid)s, %(spread_pct)s,
                %(open_interest)s, %(day_volume)s, %(delta)s, %(gamma)s,
                %(theta)s, %(vega)s, %(iv)s, %(contract_score)s)
               RETURNING id""",
            {
                "run_id":          run_id,
                "symbol_scan_id":  symbol_scan_id,
                "symbol":          data["symbol"],
                "contract_symbol": data["contract_symbol"],
                "expiry":          data["expiry"],
                "dte":             data["dte"],
                "strike":          data["strike"],
                "option_type":     data["option_type"],
                "bid":             data.get("bid"),
                "ask":             data.get("ask"),
                "mid":             data.get("mid"),
                "spread_pct":      data.get("spread_pct"),
                "open_interest":   data.get("open_interest"),
                "day_volume":      data.get("day_volume"),
                "delta":           data.get("delta"),
                "gamma":           data.get("gamma"),
                "theta":           data.get("theta"),
                "vega":            data.get("vega"),
                "iv":              data.get("iv"),
                "contract_score":  data["contract_score"],
            },
        )
        return cur.fetchone()["id"]


def get_contract_scans(run_id: int, symbol: str = None) -> list:
    with cursor() as cur:
        if symbol:
            cur.execute(
                "SELECT * FROM contract_scans WHERE run_id = %s AND symbol = %s ORDER BY contract_score DESC",
                (run_id, symbol),
            )
        else:
            cur.execute(
                "SELECT * FROM contract_scans WHERE run_id = %s ORDER BY contract_score DESC",
                (run_id,),
            )
        return cur.fetchall()


# ── IV history helpers ────────────────────────────────────────────────────────

def upsert_iv_history(symbol: str, date: str, atm_iv: float):
    with cursor() as cur:
        cur.execute(
            """INSERT INTO iv_history (symbol, date, atm_iv)
               VALUES (%s, %s, %s)
               ON CONFLICT (symbol, date) DO UPDATE SET atm_iv = EXCLUDED.atm_iv""",
            (symbol, date, atm_iv),
        )


def get_iv_history(symbol: str, days: int = 252) -> list:
    with cursor() as cur:
        cur.execute(
            """SELECT atm_iv FROM iv_history
               WHERE symbol = %s AND date >= CURRENT_DATE - INTERVAL '%s days'
               ORDER BY date ASC""",
            (symbol, days),
        )
        return [row["atm_iv"] for row in cur.fetchall()]
