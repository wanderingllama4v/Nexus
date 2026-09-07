"""
shared/llm.py — OpenAI and Perplexity client factories

All LLM calls in NEXUS go through here so model names,
timeouts, and usage tracking are centralised.
"""

import json
import os
import time

from openai import OpenAI


def _openai() -> OpenAI:
    return OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def _perplexity() -> OpenAI:
    return OpenAI(
        api_key=os.getenv("PERPLEXITY_API_KEY"),
        base_url="https://api.perplexity.ai",
    )


def call_openai(
    messages: list,
    model: str = "gpt-4o",
    json_mode: bool = True,
) -> tuple[dict | str, dict]:
    """
    Call OpenAI chat completions.
    Returns (result, usage) where result is a parsed dict if json_mode else raw string.
    usage = {tokens_in, tokens_out, latency_ms, model}
    """
    client = _openai()
    kwargs = {"model": model, "messages": messages}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}

    t0 = time.time()
    response = client.chat.completions.create(**kwargs)
    latency_ms = int((time.time() - t0) * 1000)

    content = response.choices[0].message.content
    result = json.loads(content) if json_mode else content

    usage = {
        "tokens_in":  response.usage.prompt_tokens,
        "tokens_out": response.usage.completion_tokens,
        "latency_ms": latency_ms,
        "model":      model,
    }
    return result, usage


def call_anthropic(
    messages: list,
    system: str,
    model: str = "claude-opus-4-8",
    max_tokens: int = 2048,
) -> tuple[str, dict]:
    """
    Call Anthropic Claude.
    Returns (text_response, usage).
    messages must use Anthropic format: [{"role": "user", "content": "..."}]
    """
    import anthropic
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    t0 = time.time()
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=messages,
    )
    latency_ms = int((time.time() - t0) * 1000)
    content = response.content[0].text
    return content, {
        "tokens_in":  response.usage.input_tokens,
        "tokens_out": response.usage.output_tokens,
        "latency_ms": latency_ms,
        "model":      model,
    }


def call_anthropic_json(
    messages: list,
    system: str,
    model: str = "claude-opus-4-8",
    max_tokens: int = 2048,
) -> tuple[dict, dict]:
    """
    Call Claude and parse a JSON block from the response.
    Appends a JSON instruction to the system prompt.
    Returns (parsed_dict, usage) — same signature as call_openai.
    """
    import re
    json_system = system + (
        "\n\nIMPORTANT: End your response with your structured output wrapped "
        "in a ```json code block. Do not include any text after the closing ```."
    )
    content, usage = call_anthropic(
        messages=messages,
        system=json_system,
        model=model,
        max_tokens=max_tokens,
    )
    try:
        m = re.search(r"```json\s*(\{.*?\})\s*```", content, re.DOTALL)
        if m:
            return json.loads(m.group(1)), usage
        m = re.search(r"(\{.*\})", content, re.DOTALL)
        if m:
            return json.loads(m.group(1)), usage
    except Exception:
        pass
    return {"_raw": content}, usage


def call_perplexity(
    query: str,
    model: str = "sonar-pro",
) -> tuple[str, dict]:
    """
    Call Perplexity for web-grounded research.
    Returns (text_response, usage).
    """
    client = _perplexity()
    t0 = time.time()
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a financial research assistant for an algorithmic options trading system. "
                    "Be concise and factual. Focus only on information that could move markets or "
                    "affect options pricing today. Skip general commentary."
                ),
            },
            {"role": "user", "content": query},
        ],
    )
    latency_ms = int((time.time() - t0) * 1000)
    content = response.choices[0].message.content

    usage = response.usage
    return content, {
        "tokens_in":  usage.prompt_tokens     if usage else 0,
        "tokens_out": usage.completion_tokens if usage else 0,
        "latency_ms": latency_ms,
        "model":      model,
    }
