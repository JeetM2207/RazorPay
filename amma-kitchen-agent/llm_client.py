"""Thin wrapper for calling a Claude model with forced tool use, via
OpenRouter rather than Anthropic's API directly.

This is a billing-convenience choice made during the buildathon timeline,
not an architectural one -- the model is still genuinely Claude
(anthropic/claude-sonnet-5), just reached through an OpenAI-compatible
proxy. Buyer agent scripts use this ONLY to turn natural language into a
structured cart; it never sees or influences the APPROVE/COUNTER_OFFER/
ESCALATE decision, which stays entirely inside negotiation.py.
"""

import json
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

MODEL = "anthropic/claude-sonnet-5"

_client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ["OPENROUTER_API_KEY"],
    default_headers={
        "HTTP-Referer": "https://github.com/JeetM2207/RazorPay",
        "X-Title": "Amma's Kitchen Agent",
    },
)


def call_with_forced_tool(
    user_text: str, tool_name: str, description: str, parameters: dict
) -> dict:
    """Send user_text to Claude (via OpenRouter) and force it to call the
    named tool, returning the tool call's parsed arguments."""
    tool = {
        "type": "function",
        "function": {
            "name": tool_name,
            "description": description,
            "parameters": parameters,
        },
    }
    response = _client.chat.completions.create(
        model=MODEL,
        max_tokens=300,
        messages=[{"role": "user", "content": user_text}],
        tools=[tool],
        tool_choice={"type": "function", "function": {"name": tool_name}},
    )
    message = response.choices[0].message
    if not message.tool_calls:
        raise RuntimeError("Claude (via OpenRouter) did not return a tool call")
    return json.loads(message.tool_calls[0].function.arguments)
