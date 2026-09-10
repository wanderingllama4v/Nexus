"""
components/guardian.py — GUARDIAN: Final approval gate (Claude claude-opus-4-8)

Hard-rules enforcement. No trade leaves NEXUS without GUARDIAN approval.
Reads JUDGE sized trades and SENTINEL risk flags. Applies non-negotiable limits.

Hard limits (cannot be overridden):
  - Reject all if SENTINEL portfolio_risk == CRITICAL
  - Reject any trade within earnings_blackout_days of earnings
  - Reject any trade with risk_pct > 2× the risk_per_trade budget
  - Reject if total approved risk > max_total_new_risk_pct + 1% grace

DB in:  agent_outputs (judge, sentinel, scout_earnings_watch)
DB out: agent_outputs (agent='guardian')

Standalone:
  python -m components.guardian --run-id 42
"""

import argparse
import json
from datetime import datetime

from shared import db
from shared.config import LLM, RISK
from shared.llm import call_anthropic_json


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [guardian] {msg}")


def _parse_json(row: dict) -> dict:
    if not row:
        return {}
    try:
        d = row["output_data"]
        return json.loads(d) if isinstance(d, str) else d
    except Exception:
        return {}


def _build_prompt(
    judge_trades: list,
    sentinel: dict,
    earnings_text: str,
    net_liq: float,
) -> list:
    portfolio_risk   = sentinel.get("portfolio_risk", "MEDIUM")
    risk_budget      = sentinel.get("risk_budget", {})
    risk_per_trade   = risk_budget.get("risk_per_trade_pct", RISK["risk_per_trade_pct"])
    max_total        = risk_budget.get("max_total_new_risk_pct", RISK["max_total_new_risk_pct"])
    risk_flags       = sentinel.get("risk_flags", [])
    total_new_risk   = sum(t.get("risk_pct", 0) for t in judge_trades if t.get("verdict") == "APPROVED")

    trade_block = "\n".join(
        f"  {t.get('contract_symbol','?'):<36} "
        f"x{t.get('contracts','?')}  "
        f"entry=${t.get('entry_price',0):.2f}  "
        f"stop=${t.get('stop_loss',0):.2f}  "
        f"target1=${t.get('profit_target_1') or 0:.2f}  "
        f"target2=${t.get('profit_target_2') or 0:.2f}  "
        f"risk=${t.get('dollar_risk',0):.0f} ({t.get('risk_pct',0):.2f}%)  "
        f"judge={t.get('verdict','?')}"
        for t in judge_trades
    ) or "  (none)"

    system = (
        "You are GUARDIAN, the final approval gate for an algorithmic options trading system. "
        "Your only job is to enforce hard risk limits and approve or reject each trade. "
        "You cannot approve a trade that violates a hard limit. You cannot reject a trade that "
        "clearly passes all limits — that would deny legitimate opportunities."
    )

    user = f"""HARD LIMITS (non-negotiable):
  1. CRITICAL portfolio risk → REJECT ALL
  2. earnings within {RISK['earnings_blackout_days']} day(s) → REJECT that trade
  3. single trade risk > {risk_per_trade * 2:.1f}% of account → REJECT
  4. total new risk > {max_total + 1.0:.1f}% of account (with 1% grace) → REJECT excess
  5. account below minimum ${RISK['min_account_net_liq']:,} → REJECT ALL

ACCOUNT STATE:
  Net liquidating value: ${net_liq:,.0f}
  Account minimum met:   {net_liq >= RISK['min_account_net_liq']}
  Portfolio risk level:  {portfolio_risk}
  Total proposed risk:   {total_new_risk:.2f}%
  Max allowed:           {max_total}%
  Risk flags: {risk_flags}

JUDGE-APPROVED TRADES:
{trade_block}

EARNINGS CONTEXT:
{earnings_text or '  (none available)'}

Apply each hard limit in order. For trades that pass all limits, APPROVE.
For rejected trades, state which hard limit triggered.

```json
{{
  "final_decisions": [
    {{
      "contract_symbol": "...",
      "verdict": "APPROVED | REJECTED",
      "rejection_reason": null,
      "contracts": 2,
      "entry_price": 12.55,
      "stop_loss": 8.79,
      "profit_target_1": 18.83,
      "profit_target_2": 25.10,
      "dollar_risk": 752.0,
      "risk_pct": 1.50
    }}
  ],
  "approved_count": 1,
  "rejected_count": 0,
  "hard_limits_triggered": [],
  "total_risk_approved_pct": 1.50,
  "total_risk_approved_dollars": 752.0,
  "rationale": "..."
}}
```"""

    return [{"role": "user", "content": user}]


def run(run_id: int) -> dict:
    """
    Apply hard limits and issue final APPROVED/REJECTED decisions.
    Writes to agent_outputs. Returns final decisions dict.
    """
    judge_row    = db.get_agent_output(run_id, "judge")
    sentinel_row = db.get_agent_output(run_id, "sentinel")
    earn_row     = db.get_agent_output(run_id, "scout_earnings_watch")

    judge    = _parse_json(judge_row)    if judge_row    else {}
    sentinel = _parse_json(sentinel_row) if sentinel_row else {}

    all_trades = judge.get("sized_trades", [])
    approved   = [t for t in all_trades if t.get("verdict") == "APPROVED"]

    earnings_text = ""
    if earn_row:
        try:
            d = _parse_json(earn_row)
            earnings_text = d.get("text", "")[:600] if isinstance(d, dict) else str(d)[:600]
        except Exception:
            pass

    if not approved:
        _log("No APPROVED trades from JUDGE — nothing to gate")
        result = {
            "final_decisions": [],
            "approved_count": 0,
            "rejected_count": 0,
            "hard_limits_triggered": [],
            "total_risk_approved_pct": 0,
            "total_risk_approved_dollars": 0,
            "rationale": "No approved trades from JUDGE.",
        }
        db.insert_agent_output(run_id=run_id, agent="guardian", output_data=result, score=0)
        return result

    net_liq = sentinel.get("net_liq", 0) or RISK["min_account_net_liq"]

    # Fast-path: CRITICAL risk rejects everything immediately
    if sentinel.get("portfolio_risk") == "CRITICAL":
        _log("CRITICAL portfolio risk — rejecting all trades")
        result = {
            "final_decisions": [
                {**t, "verdict": "REJECTED",
                 "rejection_reason": "CRITICAL portfolio risk — no new trades"}
                for t in approved
            ],
            "approved_count": 0,
            "rejected_count": len(approved),
            "hard_limits_triggered": ["CRITICAL_RISK"],
            "total_risk_approved_pct": 0,
            "total_risk_approved_dollars": 0,
            "rationale": "SENTINEL flagged CRITICAL portfolio risk. All trades rejected.",
        }
        db.insert_agent_output(run_id=run_id, agent="guardian", output_data=result,
                               score=0, model="local")
        return result

    messages = _build_prompt(approved, sentinel, earnings_text, net_liq)

    _log(f"Calling Claude ({LLM['sentinel']}) to apply hard limits on {len(approved)} trade(s)")
    result, usage = call_anthropic_json(
        messages=messages,
        system=(
            "You are GUARDIAN, the final approval gate for an algorithmic options trading system. "
            "Enforce hard limits precisely. Do not approve trades that violate limits."
        ),
        model=LLM["sentinel"],
        max_tokens=1200,
    )

    # Merge profit targets from JUDGE in case Claude dropped them
    judge_map = {t.get("contract_symbol"): t for t in approved}
    for d in result.get("final_decisions", []):
        if d.get("verdict") == "APPROVED":
            src = judge_map.get(d.get("contract_symbol"), {})
            if not d.get("profit_target_1") and src.get("profit_target_1"):
                d["profit_target_1"] = src["profit_target_1"]
            if not d.get("profit_target_2") and src.get("profit_target_2"):
                d["profit_target_2"] = src["profit_target_2"]

    approved_count  = result.get("approved_count", 0)
    rejected_count  = result.get("rejected_count", 0)
    limits_hit      = result.get("hard_limits_triggered", [])

    db.insert_agent_output(
        run_id=run_id, agent="guardian",
        input_data={"judge_approved": len(approved)},
        output_data=result,
        score=approved_count,
        tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
        latency_ms=usage["latency_ms"], model=usage["model"],
    )

    _log(f"Approved={approved_count}  Rejected={rejected_count}  "
         f"Limits hit={limits_hit}  "
         f"Total risk approved={result.get('total_risk_approved_pct',0):.2f}% | "
         f"[{usage['latency_ms']}ms]")

    for d in result.get("final_decisions", []):
        v = d.get("verdict", "?")
        sym = d.get("contract_symbol", "?")
        if v == "APPROVED":
            _log(f"  APPROVED  {sym}  x{d.get('contracts','?')}  "
                 f"risk=${d.get('dollar_risk',0):.0f} ({d.get('risk_pct',0):.2f}%)")
        else:
            _log(f"  REJECTED  {sym}  {d.get('rejection_reason','')}")

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEXUS GUARDIAN final approval gate")
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()
    result = run(args.run_id)
    print(f"\nGUARDIAN OUTPUT:\n{json.dumps(result, indent=2)}")
