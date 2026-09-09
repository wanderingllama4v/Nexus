"""
components/sentinel.py — SENTINEL: Portfolio risk assessment (Claude claude-opus-4-8)

Reads current account state (net_liq, open positions) and proposed trades
(from EDGE), then assesses portfolio-level risk and sets a risk budget for JUDGE.

DB in:  agent_outputs (edge, atlas, scout_earnings_watch)
DB out: agent_outputs (agent='sentinel')

Standalone:
  python -m components.sentinel --run-id 42
"""

import argparse
import json
import os
from datetime import datetime

from shared import db
from shared.config import LLM, RISK
from shared.llm import call_anthropic_json
from shared.tastytrade_client import get_account_info, get_positions

_IS_SIM = os.getenv("TT_PAPER", "true").lower() == "true"


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [sentinel] {msg}")


def _parse_json(row: dict) -> dict:
    if not row:
        return {}
    try:
        d = row["output_data"]
        return json.loads(d) if isinstance(d, str) else d
    except Exception:
        return {}


def _build_prompt(
    account: dict,
    positions: list,
    proposed_trades: list,
    atlas: dict,
    earnings_text: str,
) -> list:
    net_liq      = account.get("net_liq", 0)
    buying_power = account.get("buying_power", 0)
    regime       = atlas.get("regime", "UNKNOWN")
    risk_level   = atlas.get("risk_level", "MEDIUM")
    vix_signal   = atlas.get("vix_signal", "NEUTRAL")

    # Open positions block
    if positions:
        pos_lines = "\n".join(
            f"  {p['symbol']:<36} qty={p['quantity']}  "
            f"dir={p['direction']:<5}  avg=${p['avg_price']:.2f}  "
            f"unreal_pnl=${p['unrealized_pnl']:+.0f}"
            for p in positions
        )
    else:
        pos_lines = "  (no open option positions)"

    # Proposed trades block
    if proposed_trades:
        trade_lines = "\n".join(
            f"  {t.get('contract_symbol','?'):<36} "
            f"entry=${t.get('limit_price',0):.2f}  "
            f"stop=${t.get('stop_loss',0):.2f}  "
            f"qty={t.get('suggested_contracts',0)}  "
            f"verdict={t.get('verdict','?')}"
            for t in proposed_trades if t.get("verdict") == "GO"
        )
    else:
        trade_lines = "  (no GO trades from EDGE)"

    system = (
        "You are SENTINEL, the portfolio risk manager for an algorithmic options trading system. "
        "Your job is to assess the risk of proposed trades in the context of the current account "
        "and market conditions, then set a safe risk budget for the position sizer (JUDGE). "
        "Be conservative — protecting capital is your primary directive."
    )

    user = f"""ACCOUNT STATE:
  Net liquidating value: ${net_liq:,.0f}
  Options buying power:  ${buying_power:,.0f}
  Min account to trade:  ${RISK['min_account_net_liq']:,.0f}

OPEN POSITIONS:
{pos_lines}

MARKET REGIME:
  Regime:    {regime}
  Risk level: {risk_level}
  VIX:       {vix_signal}

PROPOSED TRADES (GO decisions from EDGE):
{trade_lines}

EARNINGS CONTEXT:
{earnings_text or '  (none available)'}

RISK FRAMEWORK:
  Default risk per trade: {RISK['risk_per_trade_pct']}% of net_liq
  Default max total risk: {RISK['max_total_new_risk_pct']}% of net_liq
  Earnings blackout:      {RISK['earnings_blackout_days']} day(s) before earnings

Assess the overall portfolio risk and each proposed trade. Consider:
1. Is the account size above minimum to trade?
2. Do any proposed trades have earnings risk within {RISK['earnings_blackout_days']} day(s)?
3. Does the market regime warrant reduced risk (HIGH/CRITICAL environment)?
4. Are there concentration issues (too many positions in one sector)?
5. What is the appropriate risk budget (may reduce from default in high-risk environments)?

```json
{{
  "portfolio_risk": "LOW | MEDIUM | HIGH | CRITICAL",
  "account_ok": true,
  "net_liq": {net_liq},
  "buying_power": {buying_power},
  "open_position_count": {len(positions)},
  "risk_budget": {{
    "risk_per_trade_pct": {RISK['risk_per_trade_pct']},
    "max_total_new_risk_pct": {RISK['max_total_new_risk_pct']},
    "reduction_reason": null
  }},
  "trade_assessments": [
    {{
      "contract_symbol": "...",
      "assessment": "ACCEPTABLE | CAUTION | REJECT",
      "reason": "..."
    }}
  ],
  "risk_flags": [],
  "rationale": "..."
}}
```"""

    return [{"role": "user", "content": user}]


def run(run_id: int) -> dict:
    """
    Assess portfolio risk for this run's proposed trades.
    Writes to agent_outputs. Returns risk assessment dict.
    """
    # Get account state — use virtual sim balance in paper/sim mode
    if _IS_SIM:
        account = {
            "net_liq":      RISK["sim_account_net_liq"],
            "buying_power": RISK["sim_buying_power"],
        }
        positions = []
    else:
        account   = get_account_info()
        positions = get_positions()
    _log(f"Account: net_liq=${account['net_liq']:,.0f}  "
         f"buying_power=${account['buying_power']:,.0f}  "
         f"open_positions={len(positions)}"
         f"{' [SIM]' if _IS_SIM else ''}")

    # Get proposed trades from EDGE
    edge_row = db.get_agent_output(run_id, "edge")
    edge     = _parse_json(edge_row) if edge_row else {}
    decisions = edge.get("decisions", [])
    go_trades = [d for d in decisions if d.get("verdict") == "GO"]

    if not go_trades:
        _log("No GO trades from EDGE — skipping risk assessment")
        result = {
            "portfolio_risk": "LOW",
            "account_ok": True,
            "net_liq": account["net_liq"],
            "buying_power": account["buying_power"],
            "open_position_count": len(positions),
            "risk_budget": {
                "risk_per_trade_pct": RISK["risk_per_trade_pct"],
                "max_total_new_risk_pct": RISK["max_total_new_risk_pct"],
                "reduction_reason": None,
            },
            "trade_assessments": [],
            "risk_flags": [],
            "rationale": "No GO trades to assess.",
        }
        db.insert_agent_output(run_id=run_id, agent="sentinel", output_data=result, score=0)
        return result

    atlas_row   = db.get_agent_output(run_id, "atlas")
    earn_row    = db.get_agent_output(run_id, "scout_earnings_watch")
    atlas       = _parse_json(atlas_row) if atlas_row else {}
    earnings_text = ""
    if earn_row:
        try:
            d = _parse_json(earn_row)
            earnings_text = d.get("text", "")[:600] if isinstance(d, dict) else str(d)[:600]
        except Exception:
            pass

    messages = _build_prompt(account, positions, go_trades, atlas, earnings_text)

    _log(f"Calling Claude ({LLM['sentinel']}) to assess {len(go_trades)} GO trade(s)")
    result, usage = call_anthropic_json(
        messages=messages,
        system=(
            "You are SENTINEL, the portfolio risk manager for an algorithmic options trading system. "
            "Protect capital above all else."
        ),
        model=LLM["sentinel"],
        max_tokens=1500,
    )

    # Ensure account info is in result even if Claude skips it
    result.setdefault("net_liq", account["net_liq"])
    result.setdefault("buying_power", account["buying_power"])
    result.setdefault("open_position_count", len(positions))

    portfolio_risk = result.get("portfolio_risk", "MEDIUM")
    risk_flags     = result.get("risk_flags", [])

    db.insert_agent_output(
        run_id=run_id, agent="sentinel",
        input_data={"go_trades": len(go_trades), "positions": len(positions)},
        output_data=result,
        score={"LOW": 90, "MEDIUM": 60, "HIGH": 30, "CRITICAL": 0}.get(portfolio_risk, 50),
        tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
        latency_ms=usage["latency_ms"], model=usage["model"],
    )

    budget = result.get("risk_budget", {})
    _log(f"Portfolio risk: {portfolio_risk} | "
         f"risk_per_trade={budget.get('risk_per_trade_pct','?')}% | "
         f"max_total={budget.get('max_total_new_risk_pct','?')}% | "
         f"flags={risk_flags} | [{usage['latency_ms']}ms]")

    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEXUS SENTINEL risk assessment")
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()
    result = run(args.run_id)
    print(f"\nSENTINEL OUTPUT:\n{json.dumps(result, indent=2)}")
