"""
Handles all Anthropic API interaction.
No Streamlit imports — this module is the portable "brain" of the app.
"""

from __future__ import annotations

import anthropic

from .catalog import SYSTEM_PROMPT

_CLIENT = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from environment

MODEL = "claude-opus-4-8"
MAX_TOKENS = 1024


def _trim_history(history: list[dict]) -> list[dict]:
    """
    Trim the message list to the last 2 complete exchanges plus the current
    user message (≤ 5 messages total).  Always starts on a user turn.
    """
    if len(history) <= 5:
        return history
    trimmed = history[-5:]
    # Guarantee the slice opens with a user message (Anthropic requirement).
    while trimmed and trimmed[0]["role"] != "user":
        trimmed = trimmed[1:]
    return trimmed


def get_answer(
    history: list[dict], use_cache: bool
) -> tuple[str, list[dict], dict]:
    """
    Send the conversation to the Anthropic API and return:
      (answer_text, trimmed_messages_sent, usage_dict)

    history  — full conversation so far, ending with the current user message.
    use_cache — when True, attaches an ephemeral cache_control breakpoint to
                the system field so the system prompt + catalog is cached.
    """
    messages = _trim_history(history)

    if use_cache:
        system: list[dict] | str = [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ]
    else:
        system = SYSTEM_PROMPT

    response = _CLIENT.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=messages,
    )

    answer = response.content[0].text
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_creation_input_tokens": getattr(
            response.usage, "cache_creation_input_tokens", 0
        ),
        "cache_read_input_tokens": getattr(
            response.usage, "cache_read_input_tokens", 0
        ),
    }
    return answer, messages, usage
