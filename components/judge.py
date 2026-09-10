"""
components/judge.py — JUDGE: Position sizing (OpenAI gpt-4o)

Reads SENTINEL's risk budget and EDGE GO decisions, then calculates
exact contract counts using fixed fractional risk management.
Math is done locally; gpt-4o validates the sizing and flags edge cases.

DB in:  agent_outputs (edge, sentinel)
DB out: agent_outputs (agent='judge')

Standalone:
  python -m components.judge --run-id 42
"""

import argparse
import json
import math
from datetime import datetime

from shared import db
from shared.config import LLM, RISK
from shared.llm import call_openai


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [judge] {msg}")


def _parse_json(row: dict) -> dict:
    if not row:
        return {}
    try:
        d = row["output_data"]
        return json.loads(d) if isinstance(d, str) else d
    except Exception:
        return {}


def _size_trade(trade: dict, net_liq: float, risk_per_trade_pct: float) -> dict:
    """
    Calculate contract count using fixed fractional risk.

    risk_dollars = net_liq * risk_per_trade_pct / 100
    stop_loss_pct = (entry - stop) / entry
    contracts = floor(risk_dollars / (entry * 100 * stop_loss_pct))
    """
    entry = trade.get("limit_price", 0)
    stop  = trade.get("stop_loss", 0)

    if not entry or not stop or entry <= stop:
        return {**trade, "contracts": 0, "dollar_risk": 0,
                "risk_pct": 0, "sizing_error": "invalid entry/stop"}

    stop_loss_pct = (entry - stop) / entry
    if stop_loss_pct <= 0:
        return {**trade, "contracts": 0, "dollar_risk": 0,
                "risk_pct": 0, "sizing_error": "stop >= entry"}

    risk_dollars  = net_liq * risk_per_trade_pct / 100.0
    raw_contracts = risk_dollars / (entry * 100 * stop_loss_pct)
    contracts     = max(1, int(math.floor(raw_contracts)))

    actual_risk_dollars = contracts * entry * 100 * stop_loss_pct
    actual_risk_pct     = actual_risk_dollars / net_liq * 100

    target1 = round(entry * 1.5, 2)
    target2 = round(entry * 2.0, 2)

    return {
        "contract_symbol":    trade.get("contract_symbol", "?"),
        "verdict":            "SIZED",
        "contracts":          contracts,
        "entry_price":        entry,
        "stop_loss":          stop,
        "profit_target_1":    trade.get("profit_target_1", target1),
        "profit_target_2":    trade.get("profit_target_2", target2),
        "dollar_risk":        round(actual_risk_dollars, 2),
        "risk_pct":           round(actual_risk_pct, 3),
        "stop_loss_pct":      round(stop_loss_pct * 100, 1),
        "sizing_error":       None,
    }


def _build_prompt(sized: list, sentinel: dict, net_liq: float, budget: dict) -> list:
    total_risk_dollars = sum(s.get("dollar_risk", 0) for s in sized)
    total_risk_pct     = sum(s.get("risk_pct", 0)    for s in sized)

    sized_block = "\n".join(
        f"  {s['contract_symbol']:<36} "
        f"contracts={s['contracts']}  "
        f"entry=${s['entry_price']:.2f}  "
        f"stop=${s['stop_loss']:.2f} ({s.get('stop_loss_pct',0):.1f}%)  "
        f"target1=${s.get('profit_target_1') or 0:.2f}  "
        f"target2=${s.get('profit_target_2') or 0:.2f}  "
        f"risk=${s['dollar_risk']:.0f} ({s['risk_pct']:.2f}%)"
        for s in sized
    )

    system = (
        "You are JUDGE, a position sizing validator for an algorithmic options trading system. "
        "The system has already calculated contract counts using fixed fractional risk. "
        "Your job is to validate the sizing, flag any issues (buying power, concentration, "
        "extreme position sizes), and confirm or adjust the final contract count."
    )

    user = f"""ACCOUNT:
  Net liquidating value: ${net_liq:,.0f}
  Portfolio risk level:  {sentinel.get('portfolio_risk', 'MEDIUM')}
  Risk budget per trade: {budget.get('risk_per_trade_pct', RISK['risk_per_trade_pct'])}%
  Max total new risk:    {budget.get('max_total_new_risk_pct', RISK['max_total_new_risk_pct'])}%
  Risk flags: {sentinel.get('risk_flags', [])}

CALCULATED SIZING:
{sized_block or '  (none)'}

TOTALS:
  Total new dollar risk: ${total_risk_dollars:,.0f}
  Total new risk %:      {total_risk_pct:.2f}%
  Max allowed total %:   {budget.get('max_total_new_risk_pct', RISK['max_total_new_risk_pct'])}%

Validate each trade:
1. If total_risk_pct exceeds the max, scale down contracts proportionally (floor)
2. If any single trade risk_pct exceeds risk_per_trade_pct, reduce contracts
3. If contracts == 0 for any trade, mark it REJECTED with reason
4. If contracts seem very high (>10) for the account size, cap at 5
5. Confirm the rest as APPROVED

Respond with a JSON object — no other text:
{{
  "sized_trades": [
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
  "total_dollar_risk": 752.0,
  "total_risk_pct": 1.50,
  "rationale": "..."
}}"""

    return [
        {"role": "system", "content": system},
        {"role": "user",   "content": user},
    ]


def run(run_id: int) -> dict:
    """
    Size positions from EDGE GO decisions using SENTINEL's risk budget.
    Writes to agent_outputs. Returns sized trades dict.
    """
    edge_row     = db.get_agent_output(run_id, "edge")
    sentinel_row = db.get_agent_output(run_id, "sentinel")

    edge     = _parse_json(edge_row)     if edge_row     else {}
    sentinel = _parse_json(sentinel_row) if sentinel_row else {}

    go_trades = [d for d in edge.get("decisions", []) if d.get("verdict") == "GO"]

    if not go_trades:
        _log("No GO trades to size")
        result = {"sized_trades": [], "total_dollar_risk": 0, "total_risk_pct": 0,
                  "rationale": "No GO trades from EDGE"}
        db.insert_agent_output(run_id=run_id, agent="judge", output_data=result, score=0)
        return result

    net_liq = sentinel.get("net_liq", 0) or RISK["min_account_net_liq"]
    budget  = sentinel.get("risk_budget", {})
    risk_pct = budget.get("risk_per_trade_pct", RISK["risk_per_trade_pct"])

    if net_liq < RISK["min_account_net_liq"]:
        _log(f"Account net_liq ${net_liq:,.0f} below minimum ${RISK['min_account_net_liq']:,.0f}")
        result = {"sized_trades": [], "total_dollar_risk": 0, "total_risk_pct": 0,
                  "rationale": f"Account below minimum ${RISK['min_account_net_liq']:,}"}
        db.insert_agent_output(run_id=run_id, agent="judge", output_data=result, score=0)
        return result

    # Local sizing math
    sized = [_size_trade(t, net_liq, risk_pct) for t in go_trades]
    _log(f"Calculated sizing for {len(sized)} trade(s) | net_liq=${net_liq:,.0f} | "
         f"risk_pct={risk_pct}%")

    messages = _build_prompt(sized, sentinel, net_liq, budget)
    _log(f"Calling OpenAI ({LLM['judge']}) to validate sizing")
    result, usage = call_openai(messages, model=LLM["judge"], json_mode=True)

    # Merge calculated profit targets in case GPT dropped them
    sized_map = {s["contract_symbol"]: s for s in sized}
    for t in result.get("sized_trades", []):
        src = sized_map.get(t.get("contract_symbol"), {})
        if not t.get("profit_target_1") and src.get("profit_target_1"):
            t["profit_target_1"] = src["profit_target_1"]
        if not t.get("profit_target_2") and src.get("profit_target_2"):
            t["profit_target_2"] = src["profit_target_2"]

    approved = [t for t in result.get("sized_trades", []) if t.get("verdict") == "APPROVED"]

    db.insert_agent_output(
        run_id=run_id, agent="judge",
        input_data={"go_trades": len(go_trades), "net_liq": net_liq},
        output_data=result,
        score=len(approved),
        tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
        latency_ms=usage["latency_ms"], model=usage["model"],
    )

    total_risk = result.get("total_risk_pct", 0)
    _log(f"Approved={len(approved)} | total_risk={total_risk:.2f}% | [{usage['latency_ms']}ms]")
    for t in result.get("sized_trades", []):
        v = t.get("verdict", "?")
        _log(f"  {v:<8} {t.get('contract_symbol','?')}  "
             f"x{t.get('contracts','?')}  "
             f"risk=${t.get('dollar_risk',0):.0f} ({t.get('risk_pct',0):.2f}%)")

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEXUS JUDGE position sizing")
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()
    result = run(args.run_id)
    print(f"\nJUDGE OUTPUT:\n{json.dumps(result, indent=2)}")
