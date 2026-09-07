"""
components/atlas.py — ATLAS: Market regime classifier (OpenAI)

Reads current market data (SPY/QQQ/IWM/VIX + sector ETFs),
incorporates SCOUT macro context, and classifies the market regime.

Regime outputs:
  BULLISH_GROWTH      — broad risk-on, growth sectors leading
  BULLISH_DEFENSIVE   — cautious risk-on, defensive rotation
  BEARISH             — broad selling, risk-off
  CHOPPY              — no clear direction, mixed signals
  RISK_OFF            — flight to safety, VIX elevated

Standalone:
  python -m components.atlas --run-id 42
"""

import argparse
import json
from datetime import datetime

from shared import db
from shared.config import LLM, SECTOR_ETFS
from shared.llm import call_openai
from shared.tastytrade_client import get_equity_quote


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [atlas] {msg}")


def _fetch_market_snapshot() -> dict:
    """Fetch prices and % changes for market indicators and sector ETFs."""
    _log("Fetching market snapshot")
    snapshot = {}

    # Core market indicators
    for ticker in ["SPY", "QQQ", "IWM", "VIX", "GLD", "TLT"]:
        q = get_equity_quote(ticker)
        if q:
            snapshot[ticker] = {
                "price":      q["price"],
                "change_pct": q["change_pct"],
            }

    # Sector ETFs
    for etf in SECTOR_ETFS:
        if etf not in snapshot:
            q = get_equity_quote(etf)
            if q:
                snapshot[etf] = {
                    "price":      q["price"],
                    "change_pct": q["change_pct"],
                    "name":       SECTOR_ETFS[etf],
                }
    return snapshot


def _build_prompt(snapshot: dict, scout_context: str) -> list:
    # Format market data for the prompt
    core = ["SPY", "QQQ", "IWM", "VIX", "GLD", "TLT"]
    core_lines = []
    for t in core:
        if t in snapshot:
            d = snapshot[t]
            core_lines.append(f"  {t:<6} {d['change_pct']:>+6.2f}%   ${d['price']:.2f}")

    sector_lines = []
    for etf, name in SECTOR_ETFS.items():
        if etf in snapshot and etf not in core:
            d = snapshot[etf]
            sector_lines.append(f"  {etf:<6} {d['change_pct']:>+6.2f}%  ({name})")

    market_block = "\n".join(core_lines)
    sector_block = "\n".join(sorted(sector_lines, key=lambda x: float(x.split()[1].rstrip("%")), reverse=True))

    system = (
        "You are ATLAS, a market regime classifier for an algorithmic options trading system. "
        "Your classification determines which sectors COMPASS will favor and whether the system "
        "looks for bullish or bearish setups. Be precise and decisive — avoid hedging language."
    )

    user = f"""Classify today's market regime based on the data below.

CORE MARKET DATA:
{market_block}

SECTOR PERFORMANCE (ranked by return):
{sector_block}

MACRO CONTEXT FROM SCOUT:
{scout_context or 'No macro context available.'}

Respond with a JSON object — no other text:
{{
  "regime": "BULLISH_GROWTH | BULLISH_DEFENSIVE | BEARISH | CHOPPY | RISK_OFF",
  "confidence": <0-100>,
  "risk_level": "LOW | MEDIUM | HIGH",
  "favored_sectors": ["XLK", "SMH", ...],
  "avoid_sectors": ["XLU", "XLRE", ...],
  "vix_signal": "FEAR | COMPLACENT | NEUTRAL",
  "breadth": "STRONG | MODERATE | WEAK | NEGATIVE",
  "rationale": "<2-3 sentence explanation>"
}}"""

    return [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ]


def run(run_id: int) -> dict:
    """
    Classify market regime and write to agent_outputs.
    Updates runs.market_regime. Returns the regime dict.
    """
    # Pull SCOUT macro context if available
    scout_rows = db.get_agent_outputs(run_id, agent_prefix="scout_")
    scout_context = ""
    for row in scout_rows:
        agent = row["agent"].replace("scout_", "").upper()
        try:
            text = json.loads(row["output_data"]).get("text", "") if isinstance(row["output_data"], str) else row["output_data"].get("text", "")
        except Exception:
            text = str(row["output_data"])
        if text:
            scout_context += f"\n[{agent}]\n{text[:600]}\n"

    snapshot = _fetch_market_snapshot()
    messages = _build_prompt(snapshot, scout_context.strip())

    _log(f"Calling OpenAI ({LLM['atlas']}) for regime classification")
    result, usage = call_openai(messages, model=LLM["atlas"], json_mode=True)

    regime = result.get("regime", "CHOPPY")
    confidence = result.get("confidence", 50)

    db.insert_agent_output(
        run_id=run_id, agent="atlas",
        input_data={"snapshot": snapshot, "scout_context_length": len(scout_context)},
        output_data=result,
        score=confidence,
        tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
        latency_ms=usage["latency_ms"], model=usage["model"],
    )
    db.update_run_regime(run_id, regime)

    _log(f"Regime: {regime} (confidence={confidence}%) | "
         f"Risk: {result.get('risk_level')} | "
         f"Favored: {result.get('favored_sectors')} | "
         f"[{usage['latency_ms']}ms]")

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEXUS ATLAS market regime classifier")
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()
    result = run(args.run_id)
    print(f"\nATLAS OUTPUT:\n{json.dumps(result, indent=2)}")
