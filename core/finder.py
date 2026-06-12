"""
Handles provider API interaction.
No Streamlit imports — this module is the portable "brain" of the app.
"""

from __future__ import annotations

from dataclasses import dataclass

import anthropic
from openai import OpenAI

from .catalog import SYSTEM_PROMPT

_ANTHROPIC_CLIENT = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
_OPENAI_CLIENT = OpenAI()  # reads OPENAI_API_KEY from environment

DEFAULT_MODEL = "claude-opus-4-8"


@dataclass(frozen=True)
class ModelConfig:
    id: str
    provider: str
    openai_cache_retention: str | None = None


# Ordered {UI label: model metadata}. All models share one byte-identical
# SYSTEM_PROMPT, but prompt caches are per-model/provider. The app therefore
# pins one model per conversation (see app.py) so cache hits stay likely.
MODELS: dict[str, ModelConfig] = {
    "Opus 4.8": ModelConfig(
        id="claude-opus-4-8",
        provider="anthropic",
    ),
    "Opus 4.7": ModelConfig(
        id="claude-opus-4-7",
        provider="anthropic",
    ),
    "Fable 5": ModelConfig(
        id="claude-fable-5",
        provider="anthropic",
    ),
    "Sonnet 4.6": ModelConfig(
        id="claude-sonnet-4-6",
        provider="anthropic",
    ),
    "GPT-5.5": ModelConfig(
        id="gpt-5.5",
        provider="openai",
        openai_cache_retention="24h",
    ),
    "GPT-5.4": ModelConfig(
        id="gpt-5.4",
        provider="openai",
        openai_cache_retention="24h",
    ),
    "GPT-5.4 mini": ModelConfig(
        id="gpt-5.4-mini",
        provider="openai",
    ),
}
_MODEL_BY_ID = {config.id: config for config in MODELS.values()}

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
    history: list[dict], use_cache: bool, model: str = DEFAULT_MODEL
) -> tuple[str, list[dict], dict]:
    """
    Send the conversation to the selected provider API and return:
      (answer_text, trimmed_messages_sent, usage_dict)

    history  — full conversation so far, ending with the current user message.
    use_cache — when True, uses the provider's prompt caching mechanism.
    model    — one of the IDs in MODELS. Caches are per-model, so a given
               conversation should keep calling with the same value.
    """
    config = _MODEL_BY_ID.get(model)
    if config is None:
        raise ValueError(f"Unknown model: {model!r}")

    messages = _trim_history(history)
    if config.provider == "anthropic":
        return _get_anthropic_answer(messages, use_cache, config)
    if config.provider == "openai":
        return _get_openai_answer(messages, use_cache, config)
    raise ValueError(f"Unknown provider: {config.provider!r}")


def _get_anthropic_answer(
    messages: list[dict], use_cache: bool, config: ModelConfig
) -> tuple[str, list[dict], dict]:
    """
    Anthropic prompt caching path. Keep this behavior unchanged.
    """

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

    response = _ANTHROPIC_CLIENT.messages.create(
        model=config.id,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=messages,
    )

    # Fable 5's safety classifiers can return stop_reason "refusal" (HTTP 200,
    # empty or partial content), so don't assume content[0] is a text block.
    if response.stop_reason == "refusal":
        answer = "Fable 5 can't help with that request. Please switch to a new model or reach out to our reference librarians at reference@law.stanford.edu or 650-725-0800"
    else:
        answer = next(
            (b.text for b in response.content if b.type == "text"), ""
        )
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_creation_input_tokens": getattr(
            response.usage, "cache_creation_input_tokens", 0
        ),
        "cache_read_input_tokens": getattr(
            response.usage, "cache_read_input_tokens", 0
        ),
        "cached_input_tokens": 0,
        "provider": config.provider,
    }
    return answer, messages, usage


def _get_openai_answer(
    messages: list[dict], use_cache: bool, config: ModelConfig
) -> tuple[str, list[dict], dict]:
    """
    OpenAI Responses API path. Prompt caching is automatic for eligible stable
    prefixes; supported models can request an explicit retention policy.
    """
    request: dict = {
        "model": config.id,
        "max_output_tokens": MAX_TOKENS,
        "input": [
            {"role": "system", "content": SYSTEM_PROMPT},
            *messages,
        ],
    }
    if use_cache and config.openai_cache_retention:
        request["prompt_cache_retention"] = config.openai_cache_retention

    response = _OPENAI_CLIENT.responses.create(**request)
    usage_obj = getattr(response, "usage", None)
    usage = {
        "input_tokens": _usage_value(usage_obj, "input_tokens", "prompt_tokens"),
        "output_tokens": _usage_value(
            usage_obj,
            "output_tokens",
            "completion_tokens",
        ),
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cached_input_tokens": _cached_input_tokens(usage_obj),
        "provider": config.provider,
    }
    return getattr(response, "output_text", ""), messages, usage


def _usage_value(obj: object, *names: str) -> int:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return 0


def _cached_input_tokens(usage_obj: object) -> int:
    details = None
    if isinstance(usage_obj, dict):
        details = (
            usage_obj.get("input_tokens_details")
            or usage_obj.get("prompt_tokens_details")
        )
    else:
        details = getattr(usage_obj, "input_tokens_details", None) or getattr(
            usage_obj,
            "prompt_tokens_details",
            None,
        )

    if isinstance(details, dict):
        return details.get("cached_tokens", 0)
    return getattr(details, "cached_tokens", 0)
