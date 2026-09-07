"""
components/compass.py — COMPASS: Sector rotation and symbol filter (OpenAI)

Reads ATLAS regime + sector ETF performance, ranks sectors,
and returns a filtered symbol list for the flow scanner and quant engine.

Standalone:
  python -m components.compass --run-id 42
"""

import argparse
import json
from datetime import datetime

from shared import db
from shared.config import LLM, SECTOR_ETFS, SYMBOL_SECTORS, SCAN_UNIVERSE
from shared.llm import call_openai
from shared.tastytrade_client import get_equity_quote


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [compass] {msg}")


def _fetch_sector_performance() -> list[dict]:
    """Fetch % change for all sector ETFs, sorted best to worst."""
    rows = []
    for etf, name in SECTOR_ETFS.items():
        q = get_equity_quote(etf)
        if q:
            rows.append({
                "etf":        etf,
                "name":       name,
                "price":      q["price"],
                "change_pct": q["change_pct"],
            })
    rows.sort(key=lambda x: x["change_pct"], reverse=True)
    return rows


def _build_prompt(atlas_output: dict, sector_perf: list[dict]) -> list:
    regime = atlas_output.get("regime", "UNKNOWN")
    favored = atlas_output.get("favored_sectors", [])
    avoid   = atlas_output.get("avoid_sectors", [])
    rationale = atlas_output.get("rationale", "")

    sector_lines = "\n".join(
        f"  {r['etf']:<6} {r['change_pct']:>+6.2f}%  ({r['name']})"
        for r in sector_perf
    )

    universe_str = ", ".join(SCAN_UNIVERSE)

    system = (
        "You are COMPASS, a sector rotation analyst for an algorithmic options trading system. "
        "Your job is to identify which sectors have the strongest momentum today and filter the "
        "symbol universe to the highest-opportunity names. Be decisive and specific."
    )

    user = f"""ATLAS market regime: {regime}
ATLAS favored sectors: {favored}
ATLAS sectors to avoid: {avoid}
ATLAS rationale: {rationale}

TODAY'S SECTOR PERFORMANCE (best to worst):
{sector_lines}

OUR FULL SYMBOL UNIVERSE:
{universe_str}

Based on the regime and sector momentum:
1. Rank the top 5 sectors for options trading today
2. From our universe, select the 8-12 symbols with the best alignment to the leading sectors
3. Flag any symbols to avoid (macro risk, earnings risk, or wrong sector)

Respond with a JSON object — no other text:
{{
  "ranked_sectors": [
    {{"etf": "XLK", "name": "Technology", "score": 94, "rationale": "..."}}
  ],
  "recommended_symbols": ["NVDA", "AAPL", ...],
  "avoid_symbols": ["SOFI", ...],
  "avoid_reason": {{"SOFI": "financials underperforming", ...}},
  "direction_bias": "BULLISH | BEARISH | MIXED",
  "rationale": "<2-3 sentence summary>"
}}"""

    return [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ]


def filter_universe(recommended: list[str], avoid: list[str]) -> list[str]:
    """
    Apply COMPASS recommendations to SCAN_UNIVERSE.
    Always includes SPY, QQQ, IWM as baseline liquidity anchors.
    """
    anchors = {"SPY", "QQQ", "IWM"}
    avoid_set = set(avoid or [])

    filtered = [s for s in SCAN_UNIVERSE
                if s in anchors or
                (s in (recommended or SCAN_UNIVERSE) and s not in avoid_set)]

    # Deduplicate while preserving order
    seen = set()
    result = []
    for s in filtered:
        if s not in seen:
            seen.add(s)
            result.append(s)
    return result


def run(run_id: int) -> dict:
    """
    Run COMPASS sector rotation analysis.
    Returns filtered symbol list and ranked sectors.
    Writes to agent_outputs.
    """
    # Read ATLAS output
    atlas_row = db.get_agent_output(run_id, "atlas")
    if not atlas_row:
        _log("No ATLAS output found — running without regime context")
        atlas_output = {"regime": "UNKNOWN", "favored_sectors": [], "avoid_sectors": []}
    else:
        try:
            atlas_output = json.loads(atlas_row["output_data"]) if isinstance(atlas_row["output_data"], str) else atlas_row["output_data"]
        except Exception:
            atlas_output = {}

    sector_perf = _fetch_sector_performance()
    messages    = _build_prompt(atlas_output, sector_perf)

    _log(f"Calling OpenAI ({LLM['compass']}) for sector rotation")
    result, usage = call_openai(messages, model=LLM["compass"], json_mode=True)

    recommended = result.get("recommended_symbols", [])
    avoid       = result.get("avoid_symbols", [])
    filtered    = filter_universe(recommended, avoid)

    result["filtered_universe"] = filtered

    db.insert_agent_output(
        run_id=run_id, agent="compass",
        input_data={"atlas_regime": atlas_output.get("regime"), "sector_count": len(sector_perf)},
        output_data=result,
        score=len(recommended),
        tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
        latency_ms=usage["latency_ms"], model=usage["model"],
    )

    top_sectors = [s["etf"] for s in result.get("ranked_sectors", [])[:3]]
    _log(f"Top sectors: {top_sectors} | "
         f"Universe: {len(filtered)} symbols ({', '.join(filtered[:6])}{'...' if len(filtered) > 6 else ''}) | "
         f"Avoiding: {avoid} | [{usage['latency_ms']}ms]")

    return result


def get_filtered_universe(run_id: int) -> list[str]:
    """
    Convenience function for the orchestrator: returns COMPASS-filtered
    symbol list for this run, falling back to full SCAN_UNIVERSE if
    COMPASS hasn't run yet.
    """
    row = db.get_agent_output(run_id, "compass")
    if not row:
        return SCAN_UNIVERSE
    try:
        data = json.loads(row["output_data"]) if isinstance(row["output_data"], str) else row["output_data"]
        return data.get("filtered_universe") or SCAN_UNIVERSE
    except Exception:
        return SCAN_UNIVERSE


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEXUS COMPASS sector rotation")
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()
    result = run(args.run_id)
    print(f"\nCOMPASS OUTPUT:\n{json.dumps(result, indent=2)}")
