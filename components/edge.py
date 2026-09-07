"""
components/edge.py — EDGE: Entry timing and execution parameters (OpenAI gpt-4o)

Reads HUNTER picks and current market conditions (time of day, VIX,
spread quality) and issues GO / NO_GO / WAIT per contract with
specific limit price, stop, and profit targets.

DB in:  agent_outputs (hunter, atlas)
DB out: agent_outputs (agent='edge')

Standalone:
  python -m components.edge --run-id 42
"""

import argparse
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from shared import db
from shared.config import LLM
from shared.llm import call_openai


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [edge] {msg}")


def _parse_json(row: dict) -> dict:
    try:
        d = row["output_data"]
        return json.loads(d) if isinstance(d, str) else d
    except Exception:
        return {}


_ET = ZoneInfo("America/New_York")


def _market_session() -> tuple[str, str]:
    """Return (session_label, time_str_ET) for the current moment."""
    now_et = datetime.now(_ET)
    time_str = now_et.strftime("%I:%M %p ET")
    total_min = now_et.hour * 60 + now_et.minute
    if total_min < 9 * 60 + 30 or total_min >= 16 * 60:
        return "CLOSED", time_str
    if total_min < 10 * 60:
        return "EARLY", time_str      # 9:30–10:00 — spreads wide
    if total_min < 15 * 60 + 30:
        return "PRIME", time_str      # 10:00–15:30 — best window
    return "LATE", time_str           # 15:30–16:00 — gamma risk rises


def _build_prompt(hunter_picks: list, session: str, time_str: str, atlas: dict) -> list:
    vix_signal = atlas.get("vix_signal", "NEUTRAL")
    risk_level = atlas.get("risk_level", "MEDIUM")
    regime     = atlas.get("regime", "UNKNOWN")

    # Format each pick for the prompt
    pick_lines = []
    for i, p in enumerate(hunter_picks, 1):
        pick_lines.append(
            f"  Pick #{i}: {p.get('contract_symbol','?')}\n"
            f"    Type={p.get('option_type','?')}  Strike=${p.get('strike',0):.2f}  "
            f"Expiry={p.get('expiry','?')}  DTE={p.get('dte','?')}\n"
            f"    bid=${p.get('bid',0):.2f}  ask=${p.get('ask',0):.2f}  mid=${p.get('mid',0):.2f}  "
            f"spread={(p.get('ask',0)-p.get('bid',0)):.2f}  spread%={(p.get('ask',0)-p.get('bid',0))/max(p.get('mid',0.01),0.01)*100:.1f}%\n"
            f"    delta={p.get('delta',0):+.2f}  IV={p.get('iv',0)*100:.1f}%  "
            f"conviction={p.get('conviction','?')}\n"
            f"    HUNTER entry=${p.get('entry_price',0):.2f}  "
            f"stop=${p.get('stop_loss',0):.2f}  target1=${p.get('profit_target_1',0):.2f}  "
            f"target2=${p.get('profit_target_2',0):.2f}\n"
            f"    Thesis: {p.get('thesis','')[:200]}"
        )
    picks_block = "\n\n".join(pick_lines) if pick_lines else "  (none)"

    system = (
        "You are EDGE, an execution timing specialist for an algorithmic options trading system. "
        "Your job is to issue final GO / NO_GO / WAIT decisions per contract pick, with precise "
        "entry parameters. Be conservative — it is better to WAIT for a better entry than to "
        "chase. If the market session is EARLY or CLOSED, return WAIT for all picks."
    )

    user = f"""MARKET CONDITIONS:
  Session:    {session}  ({time_str})
  Regime:     {regime}
  VIX signal: {vix_signal}
  Risk level: {risk_level}

SESSION GUIDANCE:
  PRIME  (10:00–15:30 ET) → execute freely
  EARLY  (9:30–10:00 ET)  → WAIT unless extraordinary conviction
  LATE   (15:30–16:00 ET) → execute only HIGH conviction, reduce size
  CLOSED                  → WAIT, all picks deferred

HUNTER PICKS TO EVALUATE:
{picks_block}

For each pick, issue GO / NO_GO / WAIT. If NO_GO, explain why. If GO:
  - Refine the limit price (use mid or 1 tick inside if spread < 2%, else at mid)
  - Confirm stop loss (~30% below entry, or HUNTER's level if reasonable)
  - Set two profit targets (1.5x and 2x entry cost)
  - Suggest number of contracts (1-5) based on conviction and risk level
    (MEDIUM risk: max 3, HIGH risk: max 2, LOW risk: up to 5)

Respond with a JSON object — no other text:
{{
  "decisions": [
    {{
      "contract_symbol": "...",
      "verdict": "GO | NO_GO | WAIT",
      "no_go_reason": null,
      "limit_price": 12.55,
      "stop_loss": 8.79,
      "profit_target_1": 18.83,
      "profit_target_2": 25.10,
      "suggested_contracts": 2,
      "timing_note": "..."
    }}
  ],
  "session": "{session}",
  "overall_go": true,
  "rationale": "..."
}}"""

    return [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ]


def run(run_id: int) -> dict:
    """
    Issue GO/NO_GO/WAIT decisions for HUNTER picks.
    Writes to agent_outputs. Returns the decisions dict.
    """
    hunter_row = db.get_agent_output(run_id, "hunter")
    if not hunter_row:
        _log("No HUNTER output found — skipping")
        result = {"decisions": [], "overall_go": False, "rationale": "No HUNTER picks"}
        db.insert_agent_output(run_id=run_id, agent="edge", output_data=result, score=0)
        return result

    hunter = _parse_json(hunter_row)
    picks = hunter.get("picks", [])

    if not picks:
        _log("HUNTER returned 0 picks — no decisions needed")
        result = {"decisions": [], "overall_go": False, "rationale": "HUNTER found no picks"}
        db.insert_agent_output(run_id=run_id, agent="edge", output_data=result, score=0)
        return result

    atlas_row = db.get_agent_output(run_id, "atlas")
    atlas     = _parse_json(atlas_row) if atlas_row else {}

    session, time_str = _market_session()
    _log(f"Session: {session} ({time_str}) | evaluating {len(picks)} picks")

    messages = _build_prompt(picks, session, time_str, atlas)
    result, usage = call_openai(messages, model=LLM["edge"], json_mode=True)

    go_count = sum(1 for d in result.get("decisions", []) if d.get("verdict") == "GO")

    db.insert_agent_output(
        run_id=run_id, agent="edge",
        input_data={"picks_evaluated": len(picks), "session": session},
        output_data=result,
        score=go_count,
        tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
        latency_ms=usage["latency_ms"], model=usage["model"],
    )

    _log(f"GO={go_count} | session={session} | overall_go={result.get('overall_go')} | [{usage['latency_ms']}ms]")
    for d in result.get("decisions", []):
        verdict = d.get("verdict", "?")
        sym = d.get("contract_symbol", "?")
        if verdict == "GO":
            _log(f"  GO     {sym}  entry=${d.get('limit_price',0):.2f}  "
                 f"stop=${d.get('stop_loss',0):.2f}  "
                 f"qty={d.get('suggested_contracts',1)}")
        else:
            _log(f"  {verdict:<6} {sym}  {d.get('no_go_reason') or d.get('timing_note','')}")

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEXUS EDGE entry timing")
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()
    result = run(args.run_id)
    print(f"\nEDGE OUTPUT:\n{json.dumps(result, indent=2)}")
