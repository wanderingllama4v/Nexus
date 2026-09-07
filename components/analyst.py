"""
components/analyst.py — ANALYST: Trade narrative brief (Claude claude-opus-4-8)

Reads the full run context (SCOUT + ATLAS + COMPASS + HUNTER + EDGE)
and writes a concise, human-readable trade brief — the final output
a human trader would read before acting on NEXUS recommendations.

DB in:  agent_outputs (all), symbol_scans, contract_scans
DB out: agent_outputs (agent='analyst')

Standalone:
  python -m components.analyst --run-id 42
"""

import argparse
import json
from datetime import datetime

from shared import db
from shared.config import LLM
from shared.llm import call_anthropic


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [analyst] {msg}")


def _parse_json(row: dict) -> dict:
    try:
        d = row["output_data"]
        return json.loads(d) if isinstance(d, str) else d
    except Exception:
        return {}


def _safe_text(row: dict, key: str = "text", limit: int = 500) -> str:
    if not row:
        return ""
    d = _parse_json(row)
    if isinstance(d, dict):
        return str(d.get(key, ""))[:limit]
    return str(d)[:limit]


def _build_context(run_id: int) -> str:
    """Compile all agent outputs for this run into a single context block."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # ATLAS
    atlas_row = db.get_agent_output(run_id, "atlas")
    atlas = _parse_json(atlas_row) if atlas_row else {}
    atlas_block = (
        f"  Regime: {atlas.get('regime','?')}  "
        f"Confidence={atlas.get('confidence','?')}%  "
        f"Risk={atlas.get('risk_level','?')}  "
        f"VIX={atlas.get('vix_signal','?')}  "
        f"Breadth={atlas.get('breadth','?')}\n"
        f"  Favored: {atlas.get('favored_sectors',[])}\n"
        f"  Avoid: {atlas.get('avoid_sectors',[])}\n"
        f"  {atlas.get('rationale','')}"
    ) if atlas else "  (not available)"

    # COMPASS
    compass_row = db.get_agent_output(run_id, "compass")
    compass = _parse_json(compass_row) if compass_row else {}
    top_sectors = [s["etf"] for s in compass.get("ranked_sectors", [])[:5]]
    compass_block = (
        f"  Bias: {compass.get('direction_bias','?')}\n"
        f"  Top sectors: {top_sectors}\n"
        f"  Active universe ({len(compass.get('filtered_universe',[]))}): "
        f"{', '.join(compass.get('filtered_universe',[]))}\n"
        f"  Avoiding: {compass.get('avoid_symbols',[])}\n"
        f"  {compass.get('rationale','')}"
    ) if compass else "  (not available)"

    # SCOUT highlights (macro + news)
    macro_row = db.get_agent_output(run_id, "scout_macro_watch")
    news_row  = db.get_agent_output(run_id, "scout_newshound")
    earn_row  = db.get_agent_output(run_id, "scout_earnings_watch")
    macro_text = _safe_text(macro_row, limit=600)
    news_text  = _safe_text(news_row,  limit=400)
    earn_text  = _safe_text(earn_row,  limit=400)

    # Symbol scans (top 5)
    scans = db.get_symbol_scans(run_id)[:5]
    scan_lines = "\n".join(
        f"  {s['symbol']:<8} score={s['technical_score']:.1f}  "
        f"dir={s['direction']:<8}  RSI={s.get('rsi',0):.1f}  "
        f"RS={s.get('relative_strength',0):+.2f}%"
        for s in scans
    ) or "  (none)"

    # HUNTER picks
    hunter_row = db.get_agent_output(run_id, "hunter")
    hunter = _parse_json(hunter_row) if hunter_row else {}
    picks = hunter.get("picks", [])
    pick_lines = "\n".join(
        f"  {i}. {p.get('contract_symbol','?')}  "
        f"entry=${p.get('entry_price',0):.2f}  "
        f"stop=${p.get('stop_loss',0):.2f}  "
        f"target=${p.get('profit_target_1',0):.2f}/{p.get('profit_target_2',0):.2f}  "
        f"conviction={p.get('conviction','?')}\n"
        f"     Thesis: {p.get('thesis','')[:200]}"
        for i, p in enumerate(picks, 1)
    ) or "  No picks identified."

    # EDGE decisions
    edge_row = db.get_agent_output(run_id, "edge")
    edge = _parse_json(edge_row) if edge_row else {}
    decisions = edge.get("decisions", [])
    decision_lines = "\n".join(
        f"  {d.get('verdict','?'):<6} {d.get('contract_symbol','?')}  "
        f"limit=${d.get('limit_price',0):.2f}  "
        f"stop=${d.get('stop_loss',0):.2f}  "
        f"qty={d.get('suggested_contracts',0)}  "
        f"{d.get('timing_note','') or d.get('no_go_reason','')}"
        for d in decisions
    ) or "  No decisions issued."
    overall_go = edge.get("overall_go", False)

    return f"""NEXUS TRADE BRIEF — {now}  (run_id={run_id})
{'='*70}

[ATLAS — MARKET REGIME]
{atlas_block}

[COMPASS — SECTOR ROTATION]
{compass_block}

[SCOUT — MACRO]
{macro_text or '  (not available)'}

[SCOUT — BREAKING NEWS]
{news_text or '  (not available)'}

[SCOUT — EARNINGS]
{earn_text or '  (not available)'}

[QUANT — TOP SYMBOLS]
{scan_lines}

[HUNTER — TRADE PICKS]
{pick_lines}

[EDGE — EXECUTION DECISIONS]
  Overall GO: {overall_go}
{decision_lines}
{'='*70}"""


def run(run_id: int) -> dict:
    """
    Write the ANALYST trade brief using Claude.
    Writes to agent_outputs. Returns the brief dict.
    """
    context = _build_context(run_id)
    _log(f"Compiling context ({len(context)} chars)")

    system = (
        "You are ANALYST, a senior options trading specialist reviewing the NEXUS algorithmic "
        "system's daily output. Write a concise, professional trade brief that a human trader "
        "would read to decide whether and how to act. Be specific: name symbols, strikes, prices. "
        "Do not pad. If EDGE says NO_GO for everything, explain why and what to watch for."
    )

    user = f"""{context}

Write a trade brief covering:
1. Market backdrop (2-3 sentences: regime, sectors, macro)
2. Setup thesis for each GO trade (or why the system is sitting out)
3. Key risk factors to watch
4. Specific action items (numbered)

Then output a JSON block at the end with this exact format (no trailing text after the JSON):
```json
{{
  "headline": "...",
  "brief": "...",
  "action_items": ["...", "..."],
  "risk_factors": ["...", "..."],
  "confidence": 75
}}
```"""

    _log(f"Calling Claude ({LLM['analyst']}) for narrative brief")
    content, usage = call_anthropic(
        messages=[{"role": "user", "content": user}],
        system=system,
        model=LLM["analyst"],
        max_tokens=2048,
    )

    # Extract the JSON block from the response
    result = _extract_json(content)
    result["full_response"] = content

    db.insert_agent_output(
        run_id=run_id, agent="analyst",
        input_data={"context_length": len(context)},
        output_data=result,
        score=result.get("confidence", 0),
        tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
        latency_ms=usage["latency_ms"], model=usage["model"],
    )

    _log(f"Brief complete | confidence={result.get('confidence','?')}% | [{usage['latency_ms']}ms]")
    _log(f"Headline: {result.get('headline','')}")

    return result


def _extract_json(text: str) -> dict:
    """Pull the JSON block from the ANALYST response."""
    try:
        # Look for ```json ... ``` block
        import re
        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            return json.loads(m.group(1))
        # Fallback: look for last { ... } block
        m = re.search(r"(\{[^{}]*\"headline\"[^{}]*\})", text, re.DOTALL)
        if m:
            return json.loads(m.group(1))
    except Exception:
        pass
    return {
        "headline": "Unable to parse structured output",
        "brief": text[:2000],
        "action_items": [],
        "risk_factors": [],
        "confidence": 0,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEXUS ANALYST trade brief")
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()
    result = run(args.run_id)
    print(f"\nANALYST OUTPUT:\n")
    print(result.get("full_response", ""))
