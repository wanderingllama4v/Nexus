"""
components/hunter.py — HUNTER: Contract selection agent (OpenAI gpt-4o)

Reads top contract_scans + regime/bias context and selects 1-3
specific contracts for potential entry, with full trade thesis.

DB in:  contract_scans, symbol_scans, agent_outputs (atlas, compass, scout_*)
DB out: agent_outputs (agent='hunter')

Standalone:
  python -m components.hunter --run-id 42
"""

import argparse
import json
from datetime import datetime

from shared import db
from shared.config import LLM
from shared.llm import call_openai


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [hunter] {msg}")


def _fmt_contracts(contracts: list, symbol_scans: list) -> str:
    """Format top contracts and their parent symbol context into a prompt block."""
    scan_map = {s["symbol"]: s for s in symbol_scans}
    lines = []
    for i, c in enumerate(contracts, 1):
        sym = c["symbol"]
        s = scan_map.get(sym, {})
        ema_str = "✓" if s.get("ema_aligned") else "✗"
        lines.append(
            f"  #{i:<2} {c['contract_symbol']:<34} "
            f"DTE={c['dte']:>3}  Strike=${c['strike']:<8.2f}  "
            f"Type={c['option_type']:<4}  Δ={c.get('delta', 0):>+.2f}  "
            f"IV={c.get('iv', 0)*100:>4.1f}%  "
            f"OI={c.get('open_interest', 0):>7,}  "
            f"Vol={c.get('day_volume', 0):>6,}  "
            f"Spread={c.get('spread_pct', 0):>4.1f}%  "
            f"Score={c['contract_score']:>5.1f}"
        )
        lines.append(
            f"       bid=${c.get('bid', 0):.2f}  "
            f"ask=${c.get('ask', 0):.2f}  "
            f"mid=${c.get('mid', 0):.2f}"
        )
        if s:
            lines.append(
                f"       Parent {sym}: score={s.get('technical_score', 0):.1f}  "
                f"dir={s.get('direction', '?')}  "
                f"RSI={s.get('rsi', 0):.1f}  "
                f"EMA={ema_str}  "
                f"vol={s.get('volume_ratio', 0):.1f}x  "
                f"RS={s.get('relative_strength', 0):+.2f}%  "
                f"IVpct={s.get('iv_percentile', 0):.0f}%"
            )
        lines.append("")
    return "\n".join(lines)


def _build_prompt(
    atlas: dict,
    compass: dict,
    contracts: list,
    symbol_scans: list,
    catalyst_text: str,
) -> list:
    regime       = atlas.get("regime", "UNKNOWN")
    confidence   = atlas.get("confidence", 50)
    risk_level   = atlas.get("risk_level", "?")
    vix_signal   = atlas.get("vix_signal", "?")
    direction    = compass.get("direction_bias", "MIXED")
    top_sectors  = [s["etf"] for s in compass.get("ranked_sectors", [])[:3]]
    avoid_syms   = compass.get("avoid_symbols", [])

    contract_block = _fmt_contracts(contracts, symbol_scans)

    system = (
        "You are HUNTER, a senior options trader for an algorithmic trading system. "
        "Your job is to select 1-3 specific contracts for potential entry — only the highest "
        "conviction setups. You are selective: if nothing is compelling, return an empty picks list. "
        "Every pick must have a clear, specific thesis and well-defined entry, stop, and target levels."
    )

    user = f"""MARKET CONTEXT:
  Regime:         {regime} (confidence={confidence}%)
  Direction bias: {direction}
  Risk level:     {risk_level}
  VIX signal:     {vix_signal}
  Leading sectors: {top_sectors}
  Symbols to avoid: {avoid_syms}

TOP CONTRACT CANDIDATES (top {len(contracts)} by scanner score):
{contract_block}
CATALYST CONTEXT:
{catalyst_text or 'No specific catalysts identified.'}

Select 1-3 contracts for potential entry. Be selective — skip anything with wide spreads (>3%),
low OI (<500), or that conflicts with the regime direction. Each pick must have a specific entry
price (use mid or 1 tick inside), a stop at ~30% below entry, and targets at 1.5x and 2x.

Respond with a JSON object — no other text:
{{
  "picks": [
    {{
      "contract_symbol": "...",
      "symbol": "NVDA",
      "option_type": "CALL",
      "strike": 900.0,
      "expiry": "2024-01-19",
      "dte": 14,
      "delta": 0.45,
      "iv": 0.65,
      "bid": 12.50,
      "ask": 12.70,
      "mid": 12.60,
      "contract_score": 87.3,
      "conviction": "HIGH | MEDIUM | LOW",
      "thesis": "...",
      "entry_price": 12.55,
      "stop_loss": 8.79,
      "profit_target_1": 18.83,
      "profit_target_2": 25.10,
      "max_risk_per_contract": 1255,
      "invalidation": "..."
    }}
  ],
  "skipped": ["AAPL240119C190 — spread too wide"],
  "rationale": "..."
}}"""

    return [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ]


def run(run_id: int, top_n: int = 15) -> dict:
    """
    Select 1-3 trade candidates from top contract_scans.
    Writes to agent_outputs. Returns the picks dict.
    """
    contracts   = db.get_contract_scans(run_id)[:top_n]
    symbol_scans = db.get_symbol_scans(run_id)

    if not contracts:
        _log("No contracts in DB for this run — nothing to pick")
        result = {"picks": [], "skipped": [], "rationale": "No contracts available"}
        db.insert_agent_output(run_id=run_id, agent="hunter", output_data=result, score=0)
        return result

    # Read ATLAS and COMPASS context
    atlas_row   = db.get_agent_output(run_id, "atlas")
    compass_row = db.get_agent_output(run_id, "compass")
    cat_row     = db.get_agent_output(run_id, "scout_catalyst_finder")

    atlas   = _parse_json(atlas_row)   if atlas_row   else {}
    compass = _parse_json(compass_row) if compass_row else {}
    catalyst_text = ""
    if cat_row:
        try:
            d = _parse_json(cat_row)
            catalyst_text = d.get("text", "") if isinstance(d, dict) else str(d)
        except Exception:
            pass

    messages = _build_prompt(atlas, compass, contracts, symbol_scans, catalyst_text[:1500])

    _log(f"Calling OpenAI ({LLM['hunter']}) with {len(contracts)} contracts")
    result, usage = call_openai(messages, model=LLM["hunter"], json_mode=True)

    picks = result.get("picks", [])
    db.insert_agent_output(
        run_id=run_id, agent="hunter",
        input_data={"contract_count": len(contracts), "regime": atlas.get("regime")},
        output_data=result,
        score=len(picks),
        tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
        latency_ms=usage["latency_ms"], model=usage["model"],
    )

    _log(f"{len(picks)} picks selected | "
         f"skipped={len(result.get('skipped', []))} | "
         f"[{usage['latency_ms']}ms]")
    for p in picks:
        _log(f"  PICK: {p['contract_symbol']}  entry=${p['entry_price']}  "
             f"stop=${p['stop_loss']}  target=${p['profit_target_1']}  "
             f"conviction={p['conviction']}")

    return result


def _parse_json(row: dict) -> dict:
    try:
        d = row["output_data"]
        return json.loads(d) if isinstance(d, str) else d
    except Exception:
        return {}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEXUS HUNTER contract selection")
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--top-n",  type=int, default=15, help="Max contracts to evaluate")
    args = parser.parse_args()
    result = run(args.run_id, top_n=args.top_n)
    print(f"\nHUNTER OUTPUT:\n{json.dumps(result, indent=2)}")
