"""
components/scout.py — SCOUT: Perplexity research agents

Five sub-agents each run a targeted web-grounded query:
  NEWSHOUND     — breaking market news (last 3 hours)
  MACRO_WATCH   — Fed, rates, macro data, risk sentiment
  EARNINGS_WATCH — earnings today/tomorrow, surprises
  EVENT_WATCH   — FDA, M&A, regulatory, geopolitical
  CATALYST_FINDER — company-specific catalysts (runs for a symbol list)

Results stored in agent_outputs. ATLAS reads MACRO_WATCH + NEWSHOUND
as context. HUNTER reads CATALYST_FINDER in Phase 3.

Standalone:
  python -m components.scout --run-id 42
  python -m components.scout --run-id 42 --agents newshound,macro_watch
"""

import argparse
from datetime import datetime

from shared import db
from shared.config import LLM
from shared.llm import call_perplexity


def _log(msg: str):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] [scout] {msg}")


# ── Sub-agent queries ─────────────────────────────────────────────────────────

def _newshound(run_id: int):
    _log("NEWSHOUND — breaking market news")
    query = (
        "What are the most important breaking market-moving news stories from the last 3 hours? "
        "Focus on: earnings surprises, analyst rating changes, major company announcements, "
        "macroeconomic data, and any events that could move individual stock prices significantly today. "
        "For each story, state: the ticker symbol (if applicable), whether it's bullish or bearish, "
        "and estimate the potential price impact (high/medium/low). Be brief and specific."
    )
    text, usage = call_perplexity(query, model=LLM["scout"])
    row_id = db.insert_agent_output(
        run_id=run_id, agent="scout_newshound",
        output_data={"text": text},
        tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
        latency_ms=usage["latency_ms"], model=usage["model"],
    )
    _log(f"NEWSHOUND done [{usage['latency_ms']}ms, {usage['tokens_out']} tokens] → id={row_id}")
    return text


def _macro_watch(run_id: int):
    _log("MACRO WATCH — Fed, rates, risk sentiment")
    query = (
        "What are today's key macro market developments? Cover: "
        "1) Federal Reserve commentary or policy signals in the last 24 hours, "
        "2) Treasury yield movements (2Y, 10Y, 30Y) and what's driving them, "
        "3) Any economic data released today (CPI, PPI, jobs, PMI, GDP etc), "
        "4) Overall risk-on vs risk-off sentiment — are investors buying or selling risk assets? "
        "5) Dollar strength and commodity moves if relevant. "
        "Be specific with numbers. This is used by an algorithmic trading system."
    )
    text, usage = call_perplexity(query, model=LLM["scout"])
    row_id = db.insert_agent_output(
        run_id=run_id, agent="scout_macro_watch",
        output_data={"text": text},
        tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
        latency_ms=usage["latency_ms"], model=usage["model"],
    )
    _log(f"MACRO WATCH done [{usage['latency_ms']}ms, {usage['tokens_out']} tokens] → id={row_id}")
    return text


def _earnings_watch(run_id: int):
    _log("EARNINGS WATCH — earnings calendar and surprises")
    query = (
        "What are today's earnings events for the stock market? Include: "
        "1) Companies reporting before market open today and their results vs expectations, "
        "2) Companies reporting after market close today, "
        "3) Companies reporting tomorrow (pre-market and after-hours), "
        "4) Any major earnings surprises or guidance changes announced in the last 12 hours. "
        "Focus on large-cap companies with high options volume: tech, semis, financials, healthcare. "
        "State EPS beat/miss and revenue beat/miss where available."
    )
    text, usage = call_perplexity(query, model=LLM["scout"])
    row_id = db.insert_agent_output(
        run_id=run_id, agent="scout_earnings_watch",
        output_data={"text": text},
        tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
        latency_ms=usage["latency_ms"], model=usage["model"],
    )
    _log(f"EARNINGS WATCH done [{usage['latency_ms']}ms, {usage['tokens_out']} tokens] → id={row_id}")
    return text


def _event_watch(run_id: int):
    _log("EVENT WATCH — FDA, M&A, regulatory, geopolitical")
    query = (
        "What significant market events are happening in the next 48 hours that options traders should know? "
        "Include: "
        "1) FOMC meeting, Fed speeches, or central bank decisions, "
        "2) Major economic data releases scheduled (with consensus estimates if available), "
        "3) FDA drug approvals or rejections expected, "
        "4) Significant M&A announcements or rumors, "
        "5) Geopolitical events that could affect energy, defense, or global supply chains. "
        "Rate each event: HIGH / MEDIUM / LOW market impact."
    )
    text, usage = call_perplexity(query, model=LLM["scout"])
    row_id = db.insert_agent_output(
        run_id=run_id, agent="scout_event_watch",
        output_data={"text": text},
        tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
        latency_ms=usage["latency_ms"], model=usage["model"],
    )
    _log(f"EVENT WATCH done [{usage['latency_ms']}ms, {usage['tokens_out']} tokens] → id={row_id}")
    return text


def _catalyst_finder(run_id: int, symbols: list[str]):
    """Runs per symbol list — called by HUNTER in Phase 3, or directly here."""
    if not symbols:
        _log("CATALYST FINDER — no symbols provided, skipping")
        return {}

    _log(f"CATALYST FINDER — {', '.join(symbols)}")
    sym_str = ", ".join(symbols)
    query = (
        f"For these stocks: {sym_str} — are there any company-specific catalysts "
        f"from the last 24 hours that could move their stock price today? "
        f"Include: analyst upgrades or downgrades (with price targets), "
        f"product announcements, partnerships or contracts, regulatory decisions, "
        f"executive changes, or any company news. "
        f"State the ticker, type of catalyst, and whether it is bullish or bearish."
    )
    text, usage = call_perplexity(query, model=LLM["scout"])
    row_id = db.insert_agent_output(
        run_id=run_id, agent="scout_catalyst_finder",
        output_data={"text": text, "symbols": symbols},
        tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
        latency_ms=usage["latency_ms"], model=usage["model"],
    )
    _log(f"CATALYST FINDER done [{usage['latency_ms']}ms, {usage['tokens_out']} tokens] → id={row_id}")
    return text


# ── All-agents runner ─────────────────────────────────────────────────────────

ALL_AGENTS = ["newshound", "macro_watch", "earnings_watch", "event_watch"]

def run(run_id: int, agents: list[str] = None, catalyst_symbols: list[str] = None) -> dict:
    """
    Run SCOUT sub-agents and write results to agent_outputs.
    agents: subset of ALL_AGENTS to run (default: all four global agents)
    catalyst_symbols: if provided, also runs CATALYST_FINDER for those symbols
    Returns dict of {agent_name: text_output}
    """
    agents = [a.lower() for a in agents] if agents else ALL_AGENTS
    results = {}

    if "newshound" in agents:
        results["newshound"] = _newshound(run_id)

    if "macro_watch" in agents:
        results["macro_watch"] = _macro_watch(run_id)

    if "earnings_watch" in agents:
        results["earnings_watch"] = _earnings_watch(run_id)

    if "event_watch" in agents:
        results["event_watch"] = _event_watch(run_id)

    if catalyst_symbols:
        results["catalyst_finder"] = _catalyst_finder(run_id, catalyst_symbols)

    _log(f"SCOUT complete — {len(results)} agents ran")
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NEXUS SCOUT research agents")
    parser.add_argument("--run-id",  type=int, required=True)
    parser.add_argument("--agents",  type=str, help="Comma-separated subset, e.g. newshound,macro_watch")
    parser.add_argument("--symbols", type=str, help="Symbols for catalyst_finder, e.g. NVDA,AAPL")
    args = parser.parse_args()

    agents  = args.agents.split(",")  if args.agents  else None
    symbols = args.symbols.split(",") if args.symbols else None
    run(args.run_id, agents=agents, catalyst_symbols=symbols)
